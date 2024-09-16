# Copyright (c) 2024, EleutherAI
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT2 style dataset."""

import os
import time

import numpy as np
import torch

from megatron import mpu, print_rank_0


class GPT2Dataset(torch.utils.data.Dataset):
    def __init__(
        self,
        name,
        data_prefix,
        documents,
        indexed_dataset,
        num_samples,
        seq_length,
        seed,
        pack_impl="packed",
        allow_chopped=True,
        build_index_mappings=True,
        use_shared_fs=True,
        label_dataset=None,
        reward_dataset=None,
        ref_dataset=None,
    ):

        self.name = name
        self.pack_impl = pack_impl
        self.allow_chopped = allow_chopped
        self.indexed_dataset = indexed_dataset
        self.label_dataset = label_dataset
        self.reward_dataset = reward_dataset
        self.ref_dataset = ref_dataset
        self.seq_length = seq_length

        # Checks
        assert self.reward_dataset is None or (
            pack_impl == "unpacked"
        ), "Reward dataset only supported with unpacked data."
        assert np.min(documents) >= 0
        assert np.max(documents) < indexed_dataset.sizes.shape[0]

        if build_index_mappings:
            # Build index mappings.
            self.doc_idx, self.sample_idx, self.shuffle_idx = _build_index_mappings(
                self.name,
                data_prefix,
                documents,
                self.indexed_dataset.sizes,
                self.label_dataset,
                num_samples,
                seq_length,
                seed,
                self.pack_impl,
                use_shared_fs=use_shared_fs,
                allow_chopped=self.allow_chopped,
            )
            self.shuffle_idx_len = self.shuffle_idx.shape[0] - 1
            self.sample_idx_len = self.sample_idx.shape[0] - 1

            if self.shuffle_idx_len != self.sample_idx_len - 1:
                print(
                    f"WARNING: shuffle index length ({self.shuffle_idx_len}) is not equal to sample index length ({self.sample_idx_len})"
                )

    def __len__(self):
        return min(self.shuffle_idx_len, self.sample_idx_len)

    def __getitem__(self, idx):
        try:
            # Get the shuffled index.
            idx = self.shuffle_idx[idx]
            # Start and end documents and offsets.
            doc_index_f = self.sample_idx[idx][0]
            doc_index_l = self.sample_idx[idx + 1][0]
            offset_f = self.sample_idx[idx][1]
            offset_l = self.sample_idx[idx + 1][1]
            # Labels and texts are supposed to be fully in sync.
            datasets = [self.indexed_dataset]
            rw_indx = 1
            if self.label_dataset is not None:
                rw_indx += 1
                datasets.append(self.label_dataset)
            if self.reward_dataset is not None:
                datasets.append(self.reward_dataset)
            else:
                rw_indx = -1
            if self.ref_dataset is not None:
                datasets.append(self.ref_dataset)
            samples = []
            sample_lengths = []
            # If we are within the same document, just extract the chunk.
            for n, dataset in enumerate(datasets):
                if doc_index_f == doc_index_l:
                    if rw_indx == n:
                        # If we are in the reward dataset, we only need the last token.
                        rw = dataset.get(self.doc_idx[doc_index_f])
                        samples.append(
                            np.array([rw[0] for _ in range(len(samples[-1]))])
                        )
                    else:
                        samples.append(
                            dataset.get(
                                self.doc_idx[doc_index_f],
                                offset=offset_l,
                                length=offset_l - offset_f + 1,
                            )
                        )
                else:
                    if n != rw_indx:
                        # reset
                        sample_lengths = []
                    # Otherwise, get the rest of the initial document.
                    if n == rw_indx:
                        rw = dataset.get(self.doc_idx[doc_index_f])
                        sample_list = [
                            np.array([rw[0] for _ in range(sample_lengths.pop(0))])
                        ]
                    else:
                        sample_list = [
                            dataset.get(self.doc_idx[doc_index_f], offset=offset_f)
                        ]
                        sample_lengths.append(len(sample_list[-1]))
                    # Loop over all in between documents and add the entire document.
                    for i in range(doc_index_f + 1, doc_index_l):
                        if n == rw_indx:
                            rw = dataset.get(self.doc_idx[i])
                            sample_list.append(
                                np.array([rw[0] for _ in range(sample_lengths.pop(0))])
                            )
                        else:
                            sample_list.append(dataset.get(self.doc_idx[i]))
                            sample_lengths.append(len(sample_list[-1]))
                    # And finally add the relevant portion of last document.
                    if n == rw_indx:
                        rw = dataset.get(self.doc_idx[doc_index_l])
                        sample_list.append(
                            np.array([rw[0] for _ in range(sample_lengths.pop(0))])
                        )
                    else:
                        sample_list.append(
                            dataset.get(self.doc_idx[doc_index_l], length=offset_l + 1)
                        )
                        sample_lengths.append(len(sample_list[-1]))
                    samples.append(np.concatenate(sample_list))
            for i in range(len(samples)):
                mask = (self.label_dataset is not None) and (i == 1)
                if len(samples[i]) < (self.seq_length + 1):
                    # Pad
                    samples[i] = np.pad(
                        samples[i],
                        (0, (self.seq_length + 1) - len(samples[i])),
                        mode="constant",
                        constant_values=-100 if mask else 0,
                    )
                elif len(samples[i]) > (self.seq_length + 1):
                    # Truncate
                    samples[i] = samples[i][: (self.seq_length + 1)]
            ret = {"text": np.array(samples[0], dtype=np.int64)}
            next_idx = 1
            if self.label_dataset is not None:
                ret["label"] = np.array(samples[next_idx], dtype=np.int64)
                next_idx += 1
            if self.reward_dataset is not None:
                ret["reward"] = np.array(samples[next_idx], dtype=np.float32)
                next_idx += 1
            if self.ref_dataset is not None:
                ret["ref"] = np.array(samples[next_idx], dtype=np.float32)
            return ret
        except IndexError as err:
            new_idx = idx % len(self)
            print(
                f"WARNING: Got index out of bounds error with index {idx} - taking modulo of index instead ({new_idx}), error: {err}"
            )
            return self[new_idx]


def _build_index_mappings(
    name,
    data_prefix,
    documents,
    sizes,
    label_dataset,
    num_samples,
    seq_length,
    seed,
    packing_impl,
    use_shared_fs=True,
    allow_chopped=True,
):
    """Build doc-idx, sample-idx, and shuffle-idx.
    doc-idx: is an array (ordered) of documents to be used in training.
    sample-idx: is the start document index and document offset for each
       training sample.
    shuffle-idx: maps the sample index into a random index into sample-idx.
    """
    # Number of tokens in each epoch and number of required epochs.
    tokens_per_epoch = _num_tokens(documents, sizes)
    num_epochs = _num_epochs(tokens_per_epoch, seq_length, num_samples)
    # rng state
    np_rng = np.random.RandomState(seed=seed)

    # Filename of the index mappings.
    _filename = data_prefix
    _filename += "_{}_indexmap".format(name)
    _filename += "_{}ns".format(num_samples)
    _filename += "_{}sl".format(seq_length)
    _filename += "_{}s".format(seed)
    _filename += "_{}pi".format(packing_impl)
    if allow_chopped:
        _filename += "_ac"
    doc_idx_filename = _filename + "_doc_idx.npy"
    sample_idx_filename = _filename + "_sample_idx.npy"
    shuffle_idx_filename = _filename + "_shuffle_idx.npy"

    if not use_shared_fs:
        should_process_dataset = int(os.environ["LOCAL_RANK"]) == 0
    else:
        should_process_dataset = torch.distributed.get_rank() == 0

    # Build the indexed mapping if not exist.
    if should_process_dataset:
        if (
            (not os.path.isfile(doc_idx_filename))
            or (not os.path.isfile(sample_idx_filename))
            or (not os.path.isfile(shuffle_idx_filename))
        ):
            print_rank_0(
                " > WARNING: could not find index map files, building "
                "the indices on rank 0 ..."
            )
            # doc-idx.
            start_time = time.time()
            if packing_impl == "packed":
                doc_idx = _build_doc_idx(documents, num_epochs, np_rng)
                np.save(doc_idx_filename, doc_idx, allow_pickle=True)
                print_rank_0(
                    " > elapsed time to build and save doc-idx mapping "
                    "(seconds): {:4f}".format(time.time() - start_time)
                )
                # sample-idx.
                start_time = time.time()
                # Use C++ implementation for speed.
                from megatron.data import helpers

                assert doc_idx.dtype == np.int32
                assert sizes.dtype == np.int32

                num_samples = (num_epochs * tokens_per_epoch - 1) / seq_length
                if 2 * (num_samples + 1) < np.iinfo(np.int32).max:
                    sample_idx = helpers.build_sample_idx_int32(
                        sizes, doc_idx, seq_length, num_epochs, tokens_per_epoch
                    )
                else:
                    sample_idx = helpers.build_sample_idx_int64(
                        sizes, doc_idx, seq_length, num_epochs, tokens_per_epoch
                    )
                np.save(sample_idx_filename, sample_idx, allow_pickle=True)
                print_rank_0(
                    " > elapsed time to build and save sample-idx mapping "
                    "(seconds): {:4f}".format(time.time() - start_time)
                )
                # shuffle-idx.
                start_time = time.time()
                # -1 is due to data structure used to retrieve the index:
                #    sample i --> [sample_idx[i], sample_idx[i+1])
                shuffle_idx = _build_shuffle_idx(sample_idx.shape[0] - 1, np_rng)
                np.save(shuffle_idx_filename, shuffle_idx, allow_pickle=True)
                print_rank_0(
                    " > elapsed time to build and save shuffle-idx mapping"
                    " (seconds): {:4f}".format(time.time() - start_time)
                )
            elif packing_impl == "pack_until_overflow":
                # Naively pack data until it overflows, then roll it over to a new one instead.
                shuffle_idx = np.arange(num_samples)  # Shuffle index around epochs
                np_rng.shuffle(shuffle_idx)
                sample_idx = []
                doc_idx = []
                # Iterate over files until we have enough samples.
                temp_shuffle_idx = np.arange(len(documents))
                np_rng.shuffle(temp_shuffle_idx)
                running_length = 0
                curr_shuffle_idx = 0
                while len(sample_idx) < num_samples:
                    if not allow_chopped:
                        # +1 since we shift left/right by 1
                        if sizes[temp_shuffle_idx[curr_shuffle_idx]] > seq_length + 1:
                            curr_shuffle_idx += 1
                            continue
                    # First, check if we need to skip this item...
                    if label_dataset is not None:
                        if np.all(
                            label_dataset.get(temp_shuffle_idx[curr_shuffle_idx])[
                                : seq_length + 1
                            ]
                            == -100
                        ):
                            curr_shuffle_idx += 1
                            continue
                    doc_length = sizes[temp_shuffle_idx[curr_shuffle_idx]]
                    if running_length == 0:
                        sample_idx.append(np.array([len(doc_idx), 0]))
                        doc_idx.append(temp_shuffle_idx[curr_shuffle_idx])
                        running_length += doc_length
                    else:
                        if running_length + doc_length > (seq_length + 1):
                            running_length = doc_length
                            sample_idx.append(np.array([len(doc_idx), 0]))
                        else:
                            running_length += doc_length
                        doc_idx.append(temp_shuffle_idx[curr_shuffle_idx])
                    curr_shuffle_idx += 1
                    if curr_shuffle_idx == len(documents):
                        curr_shuffle_idx = 0
                        np_rng.shuffle(temp_shuffle_idx)
                sample_idx.append(np.array([len(doc_idx), 0]))
                np.save(doc_idx_filename, doc_idx, allow_pickle=True)
                np.save(sample_idx_filename, sample_idx, allow_pickle=True)
                np.save(shuffle_idx_filename, shuffle_idx, allow_pickle=True)
            elif packing_impl == "unpacked":
                # Unpacked data, one sample per document.
                shuffle_idx = np.arange(num_samples)  # Shuffle index around epochs
                np_rng.shuffle(shuffle_idx)
                sample_idx = np.zeros((num_samples + 1, 2), dtype=np.int64)
                sample_idx[:, 0] = np.array([i for i in range(num_samples + 1)])
                sample_idx[:, 1] = 0
                doc_idx = list()
                doc_i = 0
                while len(doc_idx) <= num_samples:
                    if not allow_chopped:
                        # +1 since we shift left/right by 1
                        if sizes[doc_i] > seq_length + 1:
                            doc_i = (doc_i + 1) % len(documents)
                            continue
                    # Just in case we have bad data in the loop...
                    if np.all(label_dataset.get(doc_i)[:seq_length] == -100):
                        doc_i = (doc_i + 1) % len(documents)
                        continue
                    doc_idx.append(doc_i)
                    doc_i = (doc_i + 1) % len(documents)
                np.save(doc_idx_filename, doc_idx, allow_pickle=True)
                np.save(sample_idx_filename, sample_idx, allow_pickle=True)
                np.save(shuffle_idx_filename, shuffle_idx, allow_pickle=True)

    # This should be a barrier but nccl barrier assumes
    # device_index=rank which is not the case for model
    # parallel case
    counts = torch.cuda.LongTensor([1])
    torch.distributed.all_reduce(counts, group=mpu.get_io_parallel_group())
    assert counts[0].item() == torch.distributed.get_world_size(
        group=mpu.get_io_parallel_group()
    )

    # Load mappings.
    start_time = time.time()
    print_rank_0(" > loading doc-idx mapping from {}".format(doc_idx_filename))
    doc_idx = np.load(doc_idx_filename, allow_pickle=True, mmap_mode="r")
    print_rank_0(" > loading sample-idx mapping from {}".format(sample_idx_filename))
    sample_idx = np.load(sample_idx_filename, allow_pickle=True, mmap_mode="r")
    print_rank_0(" > loading shuffle-idx mapping from {}".format(shuffle_idx_filename))
    shuffle_idx = np.load(shuffle_idx_filename, allow_pickle=True, mmap_mode="r")
    print_rank_0(
        "    loaded indexed file in {:3.3f} seconds".format(time.time() - start_time)
    )
    print_rank_0("    total number of samples: {}".format(sample_idx.shape[0]))
    print_rank_0("    total number of epochs: {}".format(num_epochs))

    return doc_idx, sample_idx, shuffle_idx


def _num_tokens(documents, sizes):
    """Total number of tokens in the dataset."""
    return np.sum(sizes[documents])


def _num_epochs(tokens_per_epoch, seq_length, num_samples):
    """Based on number of samples and sequence length, calculate how many
    epochs will be needed."""
    num_epochs = 0
    total_tokens = 0
    while True:
        num_epochs += 1
        total_tokens += tokens_per_epoch
        # -1 is because we need to retrieve seq_length + 1 token each time
        # but the last token will overlap with the first token of the next
        # sample except for the last sample.
        if ((total_tokens - 1) // seq_length) >= num_samples:
            return num_epochs


def _build_doc_idx(documents, num_epochs, np_rng):
    """Build an array with length = number-of-epochs * number-of-documents.
    Each index is mapped to a corresponding document."""
    doc_idx = np.mgrid[0:num_epochs, 0 : len(documents)][1]
    doc_idx[:] = documents
    doc_idx = doc_idx.reshape(-1)
    doc_idx = doc_idx.astype(np.int32)
    np_rng.shuffle(doc_idx)
    return doc_idx


def _build_sample_idx(sizes, doc_idx, seq_length, num_epochs, tokens_per_epoch):
    """Sample index mapping is a 2D array with sizes
    [number-of-samples + 1, 2] where [..., 0] contains
    the index into `doc_idx` and [..., 1] is the
    starting offset in that document."""

    # Total number of samples. For -1 see comments in `_num_epochs`.
    num_samples = (num_epochs * tokens_per_epoch - 1) // seq_length
    sample_idx = np.zeros([num_samples + 1, 2], dtype=np.int64)

    # Index into sample_idx.
    sample_index = 0
    # Index into doc_idx.
    doc_idx_index = 0
    # Beginning offset for each document.
    doc_offset = 0
    # Start with first document and no offset.
    sample_idx[sample_index][0] = doc_idx_index
    sample_idx[sample_index][1] = doc_offset
    sample_index += 1
    while sample_index <= num_samples:
        # Start with a fresh sequence.
        remaining_seq_length = seq_length + 1
        while remaining_seq_length != 0:
            # Get the document length.
            doc_id = doc_idx[doc_idx_index]
            doc_length = sizes[doc_id] - doc_offset
            # And add it to the current sequence.
            remaining_seq_length -= doc_length
            # If we have more than a full sequence, adjust offset and set
            # remaining length to zero so we return from the while loop.
            # Note that -1 here is for the same reason we have -1 in
            # `_num_epochs` calculations.
            if remaining_seq_length <= 0:
                doc_offset += remaining_seq_length + doc_length - 1
                remaining_seq_length = 0
            else:
                # Otherwise, start from the beginning of the next document.
                doc_idx_index += 1
                doc_offset = 0
        # Record the sequence.
        sample_idx[sample_index][0] = doc_idx_index
        sample_idx[sample_index][1] = doc_offset
        sample_index += 1

    return sample_idx


def _build_shuffle_idx(size, np_rng):
    """Build the range [0, size) and shuffle."""
    dtype_ = np.uint32
    if size >= (np.iinfo(np.uint32).max - 1):
        dtype_ = np.int64
    shuffle_idx = np.arange(start=0, stop=size, step=1, dtype=dtype_)
    np_rng.shuffle(shuffle_idx)
    return shuffle_idx
