# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
import sys
import numpy as np
import time

from fairseq import utils
from fairseq.criterions.agg_softmax import AggSoftmaxCriterion
from fairseq.data import Dictionary


class SequenceScorer(object):
    """Scores the target for a given source sentence."""

    def __init__(self, tgt_dict, softmax_batch=None, compute_alignment=False, args=None):
        self.pad = tgt_dict.pad()
        self.eos = tgt_dict.eos()
        self.softmax_batch = softmax_batch or sys.maxsize
        assert self.softmax_batch > 0
        self.compute_alignment = compute_alignment
        self.args = args
        self.coef = None
        if args.pseudo_vocab_ratio > 1:
            print('Using one hot cluster distribution with K=', args.pseudo_vocab_ratio)
            # one-hot coef
            self.coef = AggSoftmaxCriterion.initialize_projection_matrix(tgt_dict, args.pseudo_vocab_ratio)
            if torch.cuda.is_available() and not args.cpu:
                self.coef = self.coef.float().cuda()
        if args.load_centroid_distribution:
            # load prior coef
            from scipy import sparse
            print('Loading cluster-token distribution from file:', args.load_centroid_distribution)
            freq_mat = sparse.load_npz(args.load_centroid_distribution).tocoo().T
            values = freq_mat.data
            indices = np.vstack((freq_mat.row, freq_mat.col))
            self.coef = torch.sparse_coo_tensor(indices, values.astype(np.float32),
                                                freq_mat.shape).coalesce()
            if torch.cuda.is_available() and not args.cpu:
                self.coef = self.coef.cuda()
        if args.num_extra_embed_file:
            print('Loading number of extra embeddings per word from file:', args.num_extra_embed_file)
            self.coef = AggSoftmaxCriterion.initialize_projection_matrix(tgt_dict, args.pseudo_vocab_ratio,
                                                                         num_extra_embed_file=args.num_extra_embed_file)
            if torch.cuda.is_available() and not args.cpu:
                self.coef = self.coef.float().cuda()

        print('coef is:')
        print(self.coef)
        if self.coef is not None:
            print('coef shape:')
            print(self.coef.shape)

    @torch.no_grad()
    def generate(self, models, sample, **kwargs):
        """Score a batch of translations."""
        net_input = sample['net_input']

        def batch_for_softmax(dec_out, target):
            # assumes decoder_out[0] is the only thing needed (may not be correct for future models!)
            first, rest = dec_out[0], dec_out[1:]
            bsz, tsz, dim = first.shape
            if bsz * tsz < self.softmax_batch:
                yield dec_out, target, True
            else:
                flat = first.contiguous().view(1, -1, dim)
                flat_tgt = target.contiguous().view(flat.shape[:-1])
                s = 0
                while s < flat.size(1):
                    e = s + self.softmax_batch
                    yield (flat[:, s:e],) + rest, flat_tgt[:, s:e], False
                    s = e

        def gather_target_probs(probs, target):
            probs = probs.gather(
                dim=2,
                index=target.unsqueeze(-1),
            )
            return probs

        def combine_knn_and_vocab_probs(knn_p, vocab_p, coeff):
            combine_probs = torch.stack([vocab_p, knn_p], dim=0)
            coeffs = torch.ones_like(combine_probs)
            coeffs[0] = np.log(1 - coeff)
            coeffs[1] = np.log(coeff)
            curr_prob = torch.logsumexp(combine_probs + coeffs, dim=0)

            return curr_prob

        orig_target = sample['target']

        # compute scores for each model in the ensemble
        avg_probs = None
        avg_attn = None
        for model in models:
            model.eval()
            decoder_out = model(**net_input)
            attn = decoder_out[1]
            if type(attn) is dict:
                attn = attn.get('attn', None)

            batched = batch_for_softmax(decoder_out, orig_target)
            probs, idx = None, 0
            for i, (bd, tgt, is_single) in enumerate(batched):
                sample['target'] = tgt
                if self.coef is None:
                    curr_prob = model.get_normalized_probs(bd, log_probs=len(models) == 1, sample=sample).data
                else:
                    curr_prob = model.get_normalized_probs(bd, log_probs=False, sample=sample).data
                    assert curr_prob.shape[0] == 1
                    curr_prob = curr_prob.squeeze(0)
                    curr_prob = torch.log(torch.clamp(torch.sparse.mm(self.coef, curr_prob.T).T, min=1e-9))
                    curr_prob = curr_prob.unsqueeze(0)

                if is_single:
                    probs = gather_target_probs(curr_prob, orig_target)
                else:
                    if probs is None:
                        probs = curr_prob.new(orig_target.numel())
                    step = curr_prob.size(0) * curr_prob.size(1)
                    end = step + idx
                    tgt_probs = gather_target_probs(curr_prob.view(tgt.shape + (curr_prob.size(-1),)), tgt)
                    probs[idx:end] = tgt_probs.view(-1)
                    idx = end
                sample['target'] = orig_target

            probs = probs.view(sample['target'].shape)

            if 'knn_dstore' in kwargs:
                dstore = kwargs['knn_dstore']
                # TxBxC
                queries = bd[1][self.args.knn_keytype]

                if len(models) != 1:
                    raise ValueError('Only knn *log* probs are supported.')

                yhat_knn_prob = dstore.get_knn_log_prob(
                    queries,
                    orig_target.permute(1, 0),
                    pad_idx=self.pad)
                yhat_knn_prob = yhat_knn_prob.permute(1, 0, 2).squeeze(-1)
                queries = queries.permute(1, 0, 2)

                if self.args.fp16:
                    yhat_knn_prob = yhat_knn_prob.half()
                    probs = probs.half()

                probs = combine_knn_and_vocab_probs(
                    yhat_knn_prob, probs, self.args.lmbda)

            if avg_probs is None:
                avg_probs = probs
            else:
                avg_probs.add_(probs)
            if attn is not None and torch.is_tensor(attn):
                attn = attn.data
                if avg_attn is None:
                    avg_attn = attn
                else:
                    avg_attn.add_(attn)

        if len(models) > 1:
            avg_probs.div_(len(models))
            avg_probs.log_()
            if avg_attn is not None:
                avg_attn.div_(len(models))

        bsz = avg_probs.size(0)
        hypos = []
        start_idxs = sample['start_indices'] if 'start_indices' in sample else [0] * bsz
        for i in range(bsz):
            # remove padding from ref
            ref = utils.strip_pad(sample['target'][i, start_idxs[i]:], self.pad) \
                if sample['target'] is not None else None
            tgt_len = ref.numel()
            avg_probs_i = avg_probs[i][start_idxs[i]:start_idxs[i] + tgt_len]
            if 'knn_dstore' in kwargs:
                knn_probs_i = yhat_knn_prob[i][start_idxs[i]:start_idxs[i] + tgt_len]
                queries_i = queries[i][start_idxs[i]:start_idxs[i] + tgt_len]
            score_i = avg_probs_i.sum() / tgt_len
            if avg_attn is not None:
                avg_attn_i = avg_attn[i]
                if self.compute_alignment:
                    alignment = utils.extract_hard_alignment(
                        avg_attn_i,
                        sample['net_input']['src_tokens'][i],
                        sample['target'][i],
                        self.pad,
                        self.eos,
                    )
                else:
                    alignment = None
            else:
                avg_attn_i = alignment = None
            hypos.append([{
                'tokens': ref,
                'score': score_i,
                'attention': avg_attn_i,
                'alignment': alignment,
                'positional_scores': avg_probs_i,
                'dstore_keys': decoder_out[1][self.args.knn_keytype][start_idxs[i]:, i,
                               :] if self.args.save_knnlm_dstore else None,
                'knn_probs': knn_probs_i if 'knn_dstore' in kwargs else None,
                'queries': queries_i if 'knn_dstore' in kwargs else None,
            }])
        return hypos
