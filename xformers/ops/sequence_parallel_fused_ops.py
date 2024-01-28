# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


import concurrent.futures
import multiprocessing.connection
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union, overload

import torch
import torch.distributed as dist
import torch.multiprocessing.reductions

from .. import _is_triton_available
from .common import BaseOperator, get_xformers_operator, register_operator

if _is_triton_available():
    from ._triton.sequence_parallel_fused_kernels import (
        BACKWARDS_WITH_ME_FIRST,
        FORWARDS_WITH_ME_LAST,
        _launch_triton_matmul,
    )

    TRITON_IS_AVAILABLE = True
else:
    TRITON_IS_AVAILABLE = False


# The sequence numbers will be communicated as 32-bit integers, due to
# limitations in both CUDA (memset can only operate on 4 bytes at a time at
# most) and Triton (scalar arguments are int32 if they fit). 32 bits are not
# enough to be sure that we'll never see overflow. Moreover, different parts of
# the code use signed or unsigned ints. To be safe, let's simulate overflow
# ourselves, at a value low enough so that it fits both a signed and an unsigned
# 32-bit integer. And, in fact, let's make it so low that we're sure we'll hit
# it in our tests, to avoid bugs that only manifest in long-running training.
SEQ_NUM_WRAP_AROUND = 2**8


@register_operator
class WriteValues(BaseOperator):
    OPERATOR = get_xformers_operator("write_values")
    OPERATOR_CATEGORY = "sequence_parallel_fused"
    NAME = "write_values"


@register_operator
class WaitValues(BaseOperator):
    OPERATOR = get_xformers_operator("wait_values")
    OPERATOR_CATEGORY = "sequence_parallel_fused"
    NAME = "wait_values"


@register_operator
class Memset32bAsync(BaseOperator):
    OPERATOR = get_xformers_operator("cuda_memset_32b_async")
    OPERATOR_CATEGORY = "sequence_parallel_fused"
    NAME = "cuda_memset_32b_async"


# We could just send tensors directly on mp.Connections, since PyTorch installs
# the necessary reductions to make it work. However, in the receiving process,
# PyTorch "mounts" the tensor in the CUDA context for the GPU with the **SAME
# INDEX** as on the sender. This works if all processes use CUDA_VISIBLE_DEVICES
# to limit themselves to a single GPU (which thus has index 0 everywhere) but in
# all other cases it's a mess. Hence we use our own reductions (which wrap the
# ones from PyTorch) to use the right devices.


def _serialize_cuda_tensor(tensor, device):
    assert tensor.device == device
    assert device.type == "cuda"
    func, args = torch.multiprocessing.reductions.reduce_tensor(tensor)
    assert func is torch.multiprocessing.reductions.rebuild_cuda_tensor
    assert args[6] == device.index
    return args


def _deserialize_cuda_tensor(args, device):
    return torch.multiprocessing.reductions.rebuild_cuda_tensor(
        *args[:6], device.index, *args[7:]
    )


# We need all processes to exchange a few strings with their addresses (in order
# to be able to connect to each other). The solution for this kind of things in
# PyTorch is a Store (TCPStore or FileStore) but we cannot create one ourselves
# (we don't know which addr/port/file to use, since the default one is already
# being used by PyTorch's global store) nor can we extract one from the
# ProcessGroup (since there's no API to do so). We thus resort to using the PG
# itself to exchange data, which is overkill (we need to store the pickled data
# into tensors and send it to the GPU). On top of that, it introduces one more
# catch: it doesn't work in inference mode because of something about modifying
# tensors inplace. I couldn't find a way to temporarily disable inference mode
# (although it's supposed to be possible) however inference mode is thread-local
# so we can dodge it by offloading the collective call to another thread. I hate
# all this so much.


def _exchange_addresses(
    listeners: List[multiprocessing.connection.Listener],
    group: dist.ProcessGroup,
    device: torch.device,
) -> List[List[str]]:
    world_size = group.size()
    my_addresses: List[str] = []
    for listener in listeners:
        addr = listener.address
        # The address could be a tuple if the listener weren't a UNIX socket
        if isinstance(addr, bytes):
            # Shouldn't be bytes, according to docs and typeshed, but...
            # https://github.com/python/typeshed/issues/10054
            addr = addr.decode("utf-8")
        assert isinstance(addr, str)
        my_addresses.append(addr)
    all_addresses = [[""] * (world_size - 1)] * world_size
    with concurrent.futures.ThreadPoolExecutor(
        initializer=torch.cuda.set_device, initargs=(device,)
    ) as e:
        e.submit(
            dist.all_gather_object,
            object_list=all_addresses,
            obj=my_addresses,
            group=group,
        ).result()
    return all_addresses


def _is_fp8_dtype(dt: torch.dtype):
    # Detect if it's float8_e4m3fn or float8_e5m2 without mentioning them in
    # order to support old versions of PyTorch that don't define them.
    return dt.is_floating_point and torch.finfo(dt).bits == 8


class _FusedSequenceParallel:
    """Set up a communication ring and perform fused ops on it

    Stores the persistent state needed to support a ring of connections between
    processes, and the logic that can do fused comms + matmuls on it.

    We want to achieve overlap between:
    - a computation which reads from the data we received from a remote GPU
    - and the communication where we send some data to another GPU
    And in order to do that we need some staging buffers and a way to
    synchronize access to them across processes.

    To perform the communication over NVLink we make the processes exchange
    their staging buffers using IPC (Inter-Process Communication) handles, which
    "mounts"/"mmaps" an allocation on one GPU into the virtual address space of
    another GPU: the memory remains backed by the original GPU but the other GPU
    can access it as if it were local. We exchange these IPC handles using
    multiprocessing Connections (and the "reductions" provided by PyTorch),
    which we establish over UNIX domain sockets, whose addresses we exchange by
    using a ProcessGroup.

    To synchronize accesses we use a set of counters/sequence numbers that are
    also allocated in memory shared over IPC handles. Processes signal that they
    completed an operation by launching a kernel that increases that value, and
    they wait for anoher process to complete an operation by launching a kernel
    that busy-waits for that value to increase. Currently we implement these
    kernels manually, but on recent CUDA drivers (515.43.04+, corresponding to
    CUDA 11.7) we could use standard stream memory operations (see
    https://docs.nvidia.com/cuda/archive/11.7.0/cuda-driver-api/group__CUDA__MEMOP.html).

    We prefer to use these kernels (or the stream memory ops) over IPC events
    because IPC events require signaling between processes at launch time to
    ensure that the wait on one process occurs after the record on another
    process. This signaling means that _launching_ our fused operation becomes a
    synchronization barrier, which can increase the launch overhead. It would
    also behave differently from NCCL, where launching is async and all the
    synchronization happens on device in the kernels. A previous version of this
    code which uses IPC events can be found here:
    https://github.com/fairinternal/xformers/pull/504.

    """

    def __init__(
        self,
        device: torch.device,
        dtype: torch.dtype,
        group: dist.ProcessGroup,
        num_stripes: int,
    ):
        self.my_device = device
        self.dtype = dtype
        self.my_rank = group.rank()
        self.world_size = group.size()
        self.num_stripes = num_stripes
        self.my_device_capability = torch.cuda.get_device_capability(self.my_device)

        # Open connections to all other processes. We exchange addresses via
        # NCCL since we don't have access to a Store.
        listeners = [
            multiprocessing.connection.Listener(family="AF_UNIX", address="", backlog=1)
            for _ in range(self.world_size - 1)
        ]
        # If any process is late, all other ones will block here
        all_addresses = _exchange_addresses(listeners, group, self.my_device)
        self.outgoing_conns = [
            None
            if r == self.my_rank
            else multiprocessing.connection.Client(
                family="AF_UNIX",
                # Mypy wants it to be str, but it actually can also be bytes
                # https://github.com/python/typeshed/issues/10054
                address=all_addresses[r][(r - self.my_rank) % self.world_size - 1],
            )
            for r in range(self.world_size)
        ]
        self.incoming_conns = [
            None
            if r == self.my_rank
            else listeners[(self.my_rank - r) % self.world_size - 1].accept()
            for r in range(self.world_size)
        ]

        self.next_stripe = 0
        self.next_seq_nums = [1] * self.num_stripes

        # My staging buffers
        self.staging = torch.empty((0,), device=self.my_device)

        # (Mmapped view of a handle to) buddies' staging buffers
        self.buddys_staging = [
            torch.empty((0,), device=self.my_device)
        ] * self.world_size

        # Allocate buffers for my inboxes
        self.num_writes_into_my_staging = torch.zeros(
            (self.world_size, self.num_stripes), dtype=torch.int, device=self.my_device
        )
        self.num_reads_from_buddys_staging = torch.zeros(
            (self.world_size, self.num_stripes), dtype=torch.int, device=self.my_device
        )

        # Send my handles to buddies
        for rank, (in_conn, out_conn) in enumerate(
            zip(self.incoming_conns, self.outgoing_conns)
        ):
            if in_conn is not None:
                in_conn.send(
                    _serialize_cuda_tensor(
                        self.num_writes_into_my_staging[rank], self.my_device
                    )
                )
            if out_conn is not None:
                out_conn.send(
                    _serialize_cuda_tensor(
                        self.num_reads_from_buddys_staging[rank], self.my_device
                    )
                )

        # Open buddies' inboxes as my outboxes
        self.num_writes_into_buddys_staging = [
            torch.empty((0,), device=self.my_device)
            if out_conn is None
            else _deserialize_cuda_tensor(out_conn.recv(), self.my_device)
            for out_conn in self.outgoing_conns
        ]
        self.num_reads_from_my_staging = [
            torch.empty((0,), device=self.my_device)
            if in_conn is None
            else _deserialize_cuda_tensor(in_conn.recv(), self.my_device)
            for in_conn in self.incoming_conns
        ]

        self.second_stream = torch.cuda.Stream()
        # CUDA can schedule the matmul and the memcpy at the same time, but it
        # tends to run the matmul first and delay the memcpy, which causes a
        # domino effect. We thus "encourage" it to prioritize the memcpy.
        self.memcpy_stream = torch.cuda.Stream(priority=-1)
        # Use dedicated streams to parallelize other operations.
        self.wait_stream = torch.cuda.Stream(priority=-1)
        self.write_stream = torch.cuda.Stream(priority=-1)

        self.next_stream_idx = 0

    def _ensure_staging_is_large_enough(self, num_elements: int, random_init: bool):
        # Lazily size up the staging area as needed. (If it's the first call,
        # this will always trigger, since staging starts empty). Once at steady
        # state, staging will be of the right (max) size and never grow again.
        if self.staging.numel() < self.world_size * num_elements:
            # When running with _memcpy=False (i.e., for benchmarks) we must
            # ensure that the staging buffer doesn't contain all zeroes as that
            # makes the matmuls go faster (better L2 compression or something).
            self.staging = torch.empty(
                (self.num_stripes, self.world_size, num_elements),
                device=self.my_device,
                dtype=self.dtype,
            )
            if random_init:
                self.staging.normal_()
            for rank, in_conn in enumerate(self.incoming_conns):
                if in_conn is not None:
                    in_conn.send(
                        _serialize_cuda_tensor(self.staging[:, rank], self.my_device)
                    )
            self.buddys_staging = [
                torch.empty((0,), device=self.my_device)
                if out_conn is None
                else _deserialize_cuda_tensor(out_conn.recv(), self.my_device)
                for rank, out_conn in enumerate(self.outgoing_conns)
            ]

    def _should_use_triton(self, _triton: bool):
        if not int(os.getenv("XFORMERS_FUSED_SEQPAR_ENABLE_TRITON", "1")):
            return False
        if not TRITON_IS_AVAILABLE:
            return False
        # Triton seems to be having issues on P100 and V100 GPUs, such as
        # https://github.com/openai/triton/issues/1609
        # https://github.com/openai/triton/issues/1610
        # https://github.com/openai/triton/issues/1257#issuecomment-1532616965
        # and, in recent Triton versions (Jan 2024), returning wrong values.
        if self.my_device_capability < (8, 0):
            return False
        if not _triton:
            return False
        return True

    def make_stream_factory(
        self, current_stream: torch.cuda.Stream
    ) -> Callable[[], torch.cuda.Stream]:
        def result():
            stream = [current_stream, self.second_stream][self.next_stream_idx]
            self.next_stream_idx += 1
            self.next_stream_idx %= 2
            return stream

        return result

    def allgather_and_linear(
        self,
        scattered_inputs: List[torch.Tensor],
        my_matmul: Callable[
            [List[torch.Tensor], int, Callable[[], torch.cuda.Stream]], None
        ],
        timeout_s: int,
        _wait: bool = True,
        _memcpy: bool = True,
        _triton: bool = True,
        _is_regular_matmul: bool = False,
        _extra_triton_args: Mapping[str, Any] = {},
    ):
        """Perform a fused all-gather followed by a linear layer"""

        assert all(si.device == self.my_device for si in scattered_inputs)
        assert all(si.dtype == self.dtype for si in scattered_inputs)

        scattered_input_numels = [si.numel() for si in scattered_inputs]
        total_scattered_input_numel = sum(scattered_input_numels)
        self._ensure_staging_is_large_enough(
            total_scattered_input_numel, random_init=_memcpy is False
        )

        stripe = self.next_stripe % self.num_stripes
        self.next_stripe += 1

        seq_num = self.next_seq_nums[stripe] % SEQ_NUM_WRAP_AROUND
        prev_seq_num = (seq_num - 1) % SEQ_NUM_WRAP_AROUND
        self.next_seq_nums[stripe] += 1

        stagings = [
            s.view((self.world_size,) + si.shape)
            for s, si in zip(
                self.staging[stripe, :, :total_scattered_input_numel].split(
                    scattered_input_numels, dim=-1
                ),
                scattered_inputs,
            )
        ]
        buddys_stagings = [
            [bs] * len(scattered_inputs)
            if bs.numel() == 0
            else [
                s.view(si.shape)
                for s, si in zip(
                    bs[stripe, :total_scattered_input_numel].split(
                        scattered_input_numels, dim=-1
                    ),
                    scattered_inputs,
                )
            ]
            for bs in self.buddys_staging
        ]

        current_stream = torch.cuda.current_stream()

        self.memcpy_stream.wait_stream(current_stream)

        # Wait for buddy to signal that it read from the data before we
        # overwrite it (this wait matches up with write [B] below).
        if _wait:
            WaitValues.OPERATOR(
                [
                    self.num_reads_from_buddys_staging[
                        (self.my_rank + iter_) % self.world_size, stripe
                    ]
                    for iter_ in range(1, self.world_size)
                ],
                prev_seq_num,
                self.memcpy_stream,
                timeout_s,
            )

        for iter_ in range(1, self.world_size):
            dst_rank = (self.my_rank + iter_) % self.world_size

            if _memcpy:
                with torch.cuda.stream(self.memcpy_stream):
                    for bs, si in zip(buddys_stagings[dst_rank], scattered_inputs):
                        bs.copy_(si)

            self.write_stream.wait_stream(self.memcpy_stream)

            # Signal to buddy that we have written into the data so it can
            # read from it (this write matches up with the wait in Triton
            # or with wait [A] below).
            if _wait:
                Memset32bAsync.OPERATOR(
                    self.num_writes_into_buddys_staging[dst_rank][stripe],
                    seq_num,
                    self.write_stream,
                )

        # If we're doing a regular matmul, we have a faster fused Triton kernel!
        if _is_regular_matmul and self._should_use_triton(_triton):
            # Wait for buddy to signal that it wrote into the data before we
            # read from it (this wait matches up with write [A] above).
            _launch_triton_matmul(
                a_my_shard=scattered_inputs[0].flatten(0, -2),
                a=stagings[0].flatten(0, -2),
                my_rank=self.my_rank,
                world_size=self.world_size,
                wait_counters=self.num_writes_into_my_staging,
                write_counters=None,
                direction=BACKWARDS_WITH_ME_FIRST,
                stripe=stripe,
                seq_num=seq_num,
                num_stripes=self.num_stripes,
                timeout_s=timeout_s,
                _wait=_wait,
                **_extra_triton_args,
            )

        else:
            # Not needed, but it prevents the waits from starting much earlier
            # than the rest of the op, which is confusing when profiling.
            self.wait_stream.wait_stream(current_stream)

            self.second_stream.wait_stream(current_stream)
            stream_factory = self.make_stream_factory(current_stream)

            my_matmul(scattered_inputs, self.my_rank, stream_factory)

            for iter_ in range(1, self.world_size):
                src_rank = (self.my_rank - iter_) % self.world_size

                # Wait for buddy to signal that it wrote into the data before we
                # read from it (this wait matches up with write [A] above).
                if _wait:
                    WaitValues.OPERATOR(
                        [self.num_writes_into_my_staging[src_rank, stripe]],
                        seq_num,
                        self.wait_stream,
                        timeout_s,
                    )
                    current_stream.wait_stream(self.wait_stream)
                    self.second_stream.wait_stream(self.wait_stream)

                my_matmul([s[src_rank] for s in stagings], src_rank, stream_factory)

            current_stream.wait_stream(self.second_stream)

        self.write_stream.wait_stream(current_stream)

        # Signal to buddy that we have read from the data so it can
        # overwrite it (this write matches up with wait [B] above).
        if _wait:
            WriteValues.OPERATOR(
                [
                    self.num_reads_from_my_staging[
                        (self.my_rank - iter_) % self.world_size
                    ][stripe]
                    for iter_ in range(1, self.world_size)
                ],
                seq_num,
                self.write_stream,
            )

    def linear_and_reducescatter(
        self,
        my_matmul: Callable[
            [List[torch.Tensor], int, Callable[[], torch.cuda.Stream]], None
        ],
        gathered_outputs: List[torch.Tensor],
        scattered_outputs: List[torch.Tensor],
        timeout_s: int,
        _wait: bool = True,
        _memcpy: bool = True,
        _triton: bool = True,
        _is_regular_matmul: bool = False,
        _extra_triton_args: Mapping[str, Any] = {},
    ):
        """Perform a fused linear layer followed by a reduce-scatter"""

        assert all(go.device == self.my_device for go in gathered_outputs)
        assert all(go.dtype == self.dtype for go in gathered_outputs)
        assert all(so.device == self.my_device for so in scattered_outputs)
        assert all(so.dtype == self.dtype for so in scattered_outputs)

        scattered_output_numels = [so.numel() for so in scattered_outputs]
        total_scattered_output_numel = sum(scattered_output_numels)
        self._ensure_staging_is_large_enough(
            total_scattered_output_numel, random_init=_memcpy is False
        )

        stripe = self.next_stripe % self.num_stripes
        self.next_stripe += 1

        seq_num = self.next_seq_nums[stripe] % SEQ_NUM_WRAP_AROUND
        prev_seq_num = (seq_num - 1) % SEQ_NUM_WRAP_AROUND
        self.next_seq_nums[stripe] += 1

        stagings = [
            s.view((self.world_size,) + so.shape)
            for s, so in zip(
                self.staging[stripe, :, :total_scattered_output_numel].split(
                    scattered_output_numels, dim=-1
                ),
                scattered_outputs,
            )
        ]
        buddys_stagings = [
            [bs] * len(scattered_outputs)
            if bs.numel() == 0
            else [
                s.view(so.shape)
                for s, so in zip(
                    bs[stripe, :total_scattered_output_numel].split(
                        scattered_output_numels, dim=-1
                    ),
                    scattered_outputs,
                )
            ]
            for bs in self.buddys_staging
        ]

        current_stream = torch.cuda.current_stream()

        self.wait_stream.wait_stream(current_stream)

        # Wait for buddy to signal that it read from the data before we
        # overwrite it (this wait matches up with write [2] below).
        if _wait:
            WaitValues.OPERATOR(
                [
                    self.num_reads_from_my_staging[
                        (self.my_rank + iter_) % self.world_size
                    ][stripe]
                    for iter_ in range(1, self.world_size)
                ],
                prev_seq_num,
                current_stream,
                timeout_s,
            )

        # If we're doing a regular matmul, we have a faster fused Triton kernel!
        if _is_regular_matmul and self._should_use_triton(_triton):
            # Signal to buddy that we have written into the data so it can
            # read from it (this write matches up with wait [1] below).
            _launch_triton_matmul(
                cs=[s.flatten(0, -2) for s in stagings],
                cs_my_shard=[
                    go[self.my_rank].flatten(0, -2) for go in gathered_outputs
                ],
                my_rank=self.my_rank,
                world_size=self.world_size,
                wait_counters=None,
                write_counters=self.num_writes_into_my_staging,
                direction=FORWARDS_WITH_ME_LAST,
                stripe=stripe,
                seq_num=seq_num,
                num_stripes=self.num_stripes,
                timeout_s=timeout_s,
                _wait=_wait,
                **_extra_triton_args,
            )

        else:
            self.second_stream.wait_stream(current_stream)
            stream_factory = self.make_stream_factory(current_stream)

            for iter_ in range(1, self.world_size):
                dst_rank = (self.my_rank + iter_) % self.world_size

                my_matmul([s[dst_rank] for s in stagings], dst_rank, stream_factory)

                # Signal to buddy that we have written into the data so it can
                # read from it (this write matches up with wait [1] below).
                if _wait:
                    self.write_stream.wait_stream(current_stream)
                    self.write_stream.wait_stream(self.second_stream)
                    WriteValues.OPERATOR(
                        [self.num_writes_into_my_staging[dst_rank, stripe]],
                        seq_num,
                        self.write_stream,
                    )

            my_matmul(
                [o[self.my_rank] for o in gathered_outputs],
                self.my_rank,
                stream_factory,
            )

            current_stream.wait_stream(self.second_stream)

        for iter_ in range(1, self.world_size):
            src_rank = (self.my_rank - iter_) % self.world_size

            # Wait for buddy to signal that it wrote into the data before we
            # read from it (this wait matches up with the write in Triton
            # or with write [1] above).
            if _wait:
                WaitValues.OPERATOR(
                    [self.num_writes_into_buddys_staging[src_rank][stripe]],
                    seq_num,
                    self.wait_stream,
                    timeout_s,
                )

            self.memcpy_stream.wait_stream(self.wait_stream)

            if _memcpy:
                with torch.cuda.stream(self.memcpy_stream):
                    for go, bs in zip(gathered_outputs, buddys_stagings[src_rank]):
                        go[src_rank].copy_(bs)

        current_stream.wait_stream(self.memcpy_stream)

        for go, so in zip(gathered_outputs, scattered_outputs):
            torch.sum(go, dim=0, out=so)

        self.write_stream.wait_stream(current_stream)

        # Signal to buddy that we have read from the data so it can
        # overwrite it (this write matches up with wait [2] above).
        if _wait:
            WriteValues.OPERATOR(
                [
                    self.num_reads_from_buddys_staging[
                        (self.my_rank - iter_) % self.world_size, stripe
                    ]
                    for iter_ in range(1, self.world_size)
                ],
                seq_num,
                self.write_stream,
            )


# We'd store this as an attribute on the PG object itself, but some PGs are
# pybind-bound classes and thus don't support it, so we simulate this as an
# external cache.
CACHE: Dict[Tuple[int, torch.dtype], Optional[_FusedSequenceParallel]] = {}


def _can_ranks_communicate_all_to_all_over_nvlink(group: dist.ProcessGroup) -> bool:
    # FIXME This is currently overly simplistic, must be improved. The following
    # should be enough:
    # - ensure that all ranks are running on the same machine (by exchanging
    #   their /proc/sys/kernel/random/boot_id value)
    # - ensure there's P2P between all pairs of ranks (can_device_access_peer
    #   could help here but it's unclear what happens if target devices aren't
    #   visible? maybe just trying to exchange IPC handles and catching errors
    #   would work? note that in any case some ranks might succeed while some
    #   might fail so we need a barrier to have them all make the same decision)
    return group.size() <= 8


def _lazy_init(
    device: torch.device, dtype: torch.dtype, group: dist.ProcessGroup, num_stripes: int
) -> Optional[_FusedSequenceParallel]:
    world_size = group.size()
    try:
        obj = CACHE[(id(group), dtype)]
    except KeyError:
        if int(os.environ.get("DISABLE_FUSED_SEQUENCE_PARALLEL", "0")):
            obj = None
        elif world_size == 1:
            obj = None
        elif not _can_ranks_communicate_all_to_all_over_nvlink(group):
            obj = None
        else:
            obj = _FusedSequenceParallel(device, dtype, group, num_stripes)
        CACHE[(id(group), dtype)] = obj
    return obj


def _default_stream_factory() -> torch.cuda.Stream:
    return torch.cuda.current_stream()


@overload
def fused_allgather_and_linear(
    scattered_input: torch.Tensor,
    weight: torch.Tensor,
    *,
    group: dist.ProcessGroup,
    out: Optional[torch.Tensor] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_scattered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> torch.Tensor:
    ...


@overload
def fused_allgather_and_linear(
    scattered_input: torch.Tensor,
    weight: List[torch.Tensor],
    *,
    group: dist.ProcessGroup,
    out: Optional[List[torch.Tensor]] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_scattered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> List[torch.Tensor]:
    ...


def fused_allgather_and_linear(
    scattered_input: torch.Tensor,
    weight: Union[torch.Tensor, List[torch.Tensor]],
    *,
    group: dist.ProcessGroup,
    out: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_scattered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> Union[torch.Tensor, List[torch.Tensor]]:
    """Performs a fused all-gather followed by a linear op

    It is equivalent to the following plain PyTorch code:

    # like scattered_input but with first dim multiplied by group's world size
    gathered_input = scattered_input.new_empty(...)
    dist.all_gather_into_tensor(gathered_input, scattered_input, group=group)
    return torch.nn.functional.linear(gathered_input, weight)

    It achieves this by breaking down the matmul into smaller partial ops (as
    many as the world size), each needing as input a different "contribution"
    to the all-gather (by a different rank), and writing to a different chunk of
    the output. Then, on one stream, it sends the local contribution to all
    other ranks (first one rank over, then two, ...) while, on another stream,
    it launches the sub-matmuls in the order in which the remote contributions
    (which are the sub-matmuls' inputs) are supposed to arrive, so that ideally
    none of the sub-matmuls will ever have to wait.

    The idea comes from this paper: https://arxiv.org/abs/2302.05442

    This method uses a staging buffer, which persists across calls, of the same
    size as the all-gathered input tensor (i.e., the input's size times the
    world size). If multiple inputs of multiple sizes are used, the staging
    buffer will be the maximum needed by any of them. Each call, when it starts,
    must first wait for the previous call to finish using the staging buffer. In
    normal conditions, where there's some other operation between two calls,
    this isn't an issue. However, when doing back-to-back calls (like in
    benchmarks) it can introduce artificial delays. To hide them, we allow using
    more than one staging buffer, which will be cycled through, thus trading
    memory for speed. This can be controlled using the num_stripes argument.

    Supports FP8 gemm for tensor-wise quantized weight and input tensors.
    To enable FP8 gemm:
    1. pass scattered_input and weight as quantized FP8 datatype
    2. pass scale_scattered_input and scale_weight, the scales used to
    quantize input and weight, respectively.
    3. set out_dtype, if not specified, will be inferred from scattered_input type.

    """
    world_size = group.size()
    weights = weight if isinstance(weight, list) else [weight]
    assert (scale_scattered_input is None) == (scale_weight is None)
    if scale_weight is not None:
        assert isinstance(weight, list) == isinstance(scale_weight, list)
        scales_weights = (
            scale_weight if isinstance(scale_weight, list) else [scale_weight]
        )
        assert len(weights) == len(scales_weights)
        assert out_dtype is not None, "output_dtype is required with FP8"
    else:
        scales_weights = [torch.empty(1)] * len(weights)
    assert all(w.ndim == 2 for w in weights)
    assert scattered_input.ndim >= 2
    assert all(scattered_input.shape[-1] == w.shape[-1] for w in weights)
    assert scattered_input.is_contiguous()
    gathered_input_shape = (world_size,) + scattered_input.shape
    gathered_output_shapes = [gathered_input_shape[:-1] + w.shape[:-1] for w in weights]
    if out is not None:
        assert isinstance(out, list) == isinstance(weight, list)
        gathered_outputs = out if isinstance(out, list) else [out]
        assert len(gathered_outputs) == len(gathered_output_shapes)
        assert all(
            go.shape == gos for go, gos in zip(gathered_outputs, gathered_output_shapes)
        )
        assert all(go.is_contiguous() for go in gathered_outputs)
        if out_dtype is not None:
            if isinstance(out, list):
                for o in out:
                    assert o.dtype == out_dtype
            else:
                assert out.dtype == out_dtype
    else:
        gathered_outputs = [
            scattered_input.new_empty(
                gos,
                dtype=out_dtype if out_dtype is not None else scattered_input.dtype,
            )
            for gos in gathered_output_shapes
        ]

    def my_matmul(
        inputs: List[torch.Tensor],
        src_rank: int,
        stream_factory: Callable[[], torch.cuda.Stream],
    ) -> None:
        for w, scale_weight, go in zip(weights, scales_weights, gathered_outputs):
            with torch.cuda.stream(stream_factory()):
                if _is_fp8_dtype(w.dtype):
                    output_amax = torch.empty(1, dtype=torch.float32, device=w.device)
                    torch._scaled_mm(
                        inputs[0],
                        w.t(),
                        out_dtype=go[src_rank].dtype,
                        scale_a=scale_scattered_input,
                        scale_b=scale_weight,
                        out=(go[src_rank], output_amax),
                    )
                else:
                    torch.matmul(inputs[0], w.t(), out=go[src_rank])

    _is_regular_matmul = all([not _is_fp8_dtype(w.dtype) for w in weights])
    fused_allgather_and_anything(
        [scattered_input],
        my_matmul,
        group=group,
        num_stripes=num_stripes,
        timeout_s=timeout_s,
        _is_regular_matmul=_is_regular_matmul,
        _extra_triton_args=dict(
            bs=[w.t() for w in weights],
            cs=[go.flatten(0, -2) for go in gathered_outputs],
            cs_my_shard=None,
        ),
        **private_args_DO_NOT_USE,
    )

    if isinstance(weight, list):
        return [go.flatten(0, 1) for go in gathered_outputs]
    else:
        return gathered_outputs[0].flatten(0, 1)


def fused_allgather_and_anything(
    scattered_inputs: List[torch.Tensor],
    my_matmul: Callable[
        [List[torch.Tensor], int, Callable[[], torch.cuda.Stream]], None
    ],
    *,
    group: dist.ProcessGroup,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    **private_args_DO_NOT_USE,
) -> None:
    world_size = group.size()

    if len(scattered_inputs) == 0:
        for src_rank in range(world_size):
            my_matmul([], src_rank, _default_stream_factory)
        return

    assert all(si.is_contiguous() for si in scattered_inputs)
    assert all(si.device == scattered_inputs[0].device for si in scattered_inputs)
    assert all(si.dtype == scattered_inputs[0].dtype for si in scattered_inputs)

    gathered_input_shapes = [(world_size,) + si.shape for si in scattered_inputs]

    obj = _lazy_init(
        scattered_inputs[0].device, scattered_inputs[0].dtype, group, num_stripes
    )

    if world_size == 1:
        my_matmul(scattered_inputs, 0, _default_stream_factory)

    # Fallback
    elif obj is None:
        gathered_inputs = [
            si.new_empty(gis)
            for si, gis in zip(scattered_inputs, gathered_input_shapes)
        ]
        for si, gi in zip(scattered_inputs, gathered_inputs):
            dist.all_gather_into_tensor(output_tensor=gi, input_tensor=si, group=group)
        for src_rank in range(world_size):
            my_matmul(
                [gi[src_rank] for gi in gathered_inputs],
                src_rank,
                _default_stream_factory,
            )

    # Fast path
    else:
        assert scattered_inputs[0].device == obj.my_device
        assert scattered_inputs[0].dtype == obj.dtype
        assert obj.num_stripes == num_stripes
        obj.allgather_and_linear(
            scattered_inputs,
            my_matmul,
            timeout_s=timeout_s,
            _wait=private_args_DO_NOT_USE.get("_wait", True),
            _memcpy=private_args_DO_NOT_USE.get("_memcpy", True),
            _triton=private_args_DO_NOT_USE.get("_triton", True),
            _is_regular_matmul=private_args_DO_NOT_USE.get("_is_regular_matmul", False),
            _extra_triton_args=private_args_DO_NOT_USE.get("_extra_triton_args", {}),
        )


@overload
def fused_linear_and_reducescatter(
    gathered_input: torch.Tensor,
    weight: torch.Tensor,
    *,
    group: dist.ProcessGroup,
    out: Optional[torch.Tensor] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_gathered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> torch.Tensor:
    ...


@overload
def fused_linear_and_reducescatter(
    gathered_input: torch.Tensor,
    weight: List[torch.Tensor],
    *,
    group: dist.ProcessGroup,
    out: Optional[List[torch.Tensor]] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_gathered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> List[torch.Tensor]:
    ...


def fused_linear_and_reducescatter(
    gathered_input: torch.Tensor,
    weight: Union[torch.Tensor, List[torch.Tensor]],
    *,
    group: dist.ProcessGroup,
    out: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    scale_gathered_input: Optional[torch.Tensor] = None,
    scale_weight: Optional[Union[torch.Tensor, List[torch.Tensor]]] = None,
    out_dtype: Optional[torch.dtype] = None,
    **private_args_DO_NOT_USE,
) -> Union[torch.Tensor, List[torch.Tensor]]:
    """Performs a fused linear op followed by a reduce-scatter

    It is equivalent to the following plain PyTorch code:

    gathered_output = torch.nn.functional.linear(gathered_input, weight)
    # like gathered_output but with first dim divided by group's world size
    scattered_output = gathered_output.new_empty(...)
    dist.reduce_scatter_tensor(scattered_output, gathered_output, group=group)

    Supports FP8 gemm with tensor-wise quantized weights. To enable FP8 gemm:
    1. pass weight and gathered_input as FP8 tensors
    2. Set `scale_gathered_input` and `scale_weight` to the scales used to quantize
    inputs and weight, respectively.
    3. Set out_dtype to the desired output dtype. If not specified, it will be inferred from
    gathered_input datatype.
    """
    world_size = group.size()
    weights = weight if isinstance(weight, list) else [weight]
    assert (scale_gathered_input is None) == (scale_weight is None)
    if scale_weight is not None:
        assert isinstance(weight, list) == isinstance(scale_weight, list)
        scales_weights = (
            scale_weight if isinstance(scale_weight, list) else [scale_weight]
        )
        assert len(weights) == len(scales_weights)
        assert out_dtype is not None, "output_dtype is required with FP8"
    else:
        scales_weights = [torch.empty(1)] * len(weights)
    assert all(w.ndim == 2 for w in weights)
    assert gathered_input.ndim >= 2
    assert all(gathered_input.shape[-1] == w.shape[-1] for w in weights)
    assert gathered_input.is_contiguous()
    assert gathered_input.shape[0] % world_size == 0
    gathered_input = gathered_input.view(
        (world_size, gathered_input.shape[0] // world_size) + gathered_input.shape[1:]
    )
    gathered_output_shapes = [gathered_input.shape[:-1] + w.shape[:-1] for w in weights]
    scattered_output_shapes = [gos[1:] for gos in gathered_output_shapes]
    if out is not None:
        assert isinstance(out, list) == isinstance(weight, list)
        scattered_outputs = out if isinstance(out, list) else [out]
        assert len(scattered_outputs) == scattered_output_shapes
        assert all(so.device == gathered_input.device for so in scattered_outputs)
        assert all(so.dtype == gathered_input.dtype for so in scattered_outputs)
        assert all(
            so.shape == sos
            for so, sos in zip(scattered_outputs, scattered_output_shapes)
        )
        if out_dtype is not None:
            if isinstance(out, list):
                for o in out:
                    assert o.dtype == out_dtype
            else:
                assert out.dtype == out_dtype
    else:
        scattered_outputs = [
            gathered_input.new_empty(
                sos,
                dtype=out_dtype if out_dtype is not None else gathered_input.dtype,
            )
            for sos in scattered_output_shapes
        ]

    def my_matmul(
        outputs: List[torch.Tensor],
        dst_rank: int,
        stream_factory: Callable[[], torch.cuda.Stream],
    ) -> None:
        for w, scale_weight, o in zip(weights, scales_weights, outputs):
            with torch.cuda.stream(stream_factory()):
                if _is_fp8_dtype(w.dtype):
                    output_amax = torch.empty(1, dtype=torch.float32, device=o.device)
                    torch._scaled_mm(
                        gathered_input[dst_rank],
                        w.t(),
                        out_dtype=o.dtype,
                        scale_a=scale_gathered_input,
                        scale_b=scale_weight,
                        out=(o, output_amax),
                    )
                else:
                    torch.matmul(gathered_input[dst_rank], w.t(), out=o)

    _is_regular_matmul = all([not _is_fp8_dtype(w.dtype) for w in weights])
    fused_anything_and_reducescatter(
        my_matmul,
        scattered_outputs,
        group=group,
        num_stripes=num_stripes,
        timeout_s=timeout_s,
        _is_regular_matmul=_is_regular_matmul,
        _extra_triton_args=dict(
            a_my_shard=None,
            a=gathered_input.flatten(0, -2),
            bs=[w.t() for w in weights],
        ),
        **private_args_DO_NOT_USE,
    )

    if isinstance(weight, list):
        return scattered_outputs
    else:
        return scattered_outputs[0]


def fused_anything_and_reducescatter(
    my_matmul: Callable[
        [List[torch.Tensor], int, Callable[[], torch.cuda.Stream]], None
    ],
    scattered_outputs: List[torch.Tensor],
    *,
    group: dist.ProcessGroup,
    num_stripes: int = 1,
    timeout_s: int = 60 * 60,
    **private_args_DO_NOT_USE,
) -> None:
    world_size = group.size()

    if len(scattered_outputs) == 0:
        for dst_rank in range(world_size):
            my_matmul([], dst_rank, _default_stream_factory)
        return

    assert all(so.is_contiguous() for so in scattered_outputs)
    assert all(so.device == scattered_outputs[0].device for so in scattered_outputs)
    assert all(so.dtype == scattered_outputs[0].dtype for so in scattered_outputs)

    gathered_output_shapes = [(world_size,) + so.shape for so in scattered_outputs]

    obj = _lazy_init(
        scattered_outputs[0].device, scattered_outputs[0].dtype, group, num_stripes
    )

    if world_size == 1:
        my_matmul(scattered_outputs, 0, _default_stream_factory)

    # Fallback
    elif obj is None:
        gathered_outputs = [
            so.new_empty(gos)
            for so, gos in zip(scattered_outputs, gathered_output_shapes)
        ]
        for dst_rank in range(world_size):
            my_matmul(
                [go[dst_rank] for go in gathered_outputs],
                dst_rank,
                _default_stream_factory,
            )
        for go, so in zip(gathered_outputs, scattered_outputs):
            dist.reduce_scatter_tensor(output=so, input=go, group=group)

    # Fast path
    else:
        assert scattered_outputs[0].device == obj.my_device
        assert scattered_outputs[0].dtype == obj.dtype
        assert obj.num_stripes == num_stripes
        gathered_outputs = [
            scattered_outputs[0].new_empty(gos) for gos in gathered_output_shapes
        ]
        obj.linear_and_reducescatter(
            my_matmul,
            gathered_outputs,
            scattered_outputs,
            timeout_s=timeout_s,
            _wait=private_args_DO_NOT_USE.get("_wait", True),
            _memcpy=private_args_DO_NOT_USE.get("_memcpy", True),
            _triton=private_args_DO_NOT_USE.get("_triton", True),
            _is_regular_matmul=private_args_DO_NOT_USE.get("_is_regular_matmul", False),
            _extra_triton_args=private_args_DO_NOT_USE.get("_extra_triton_args", {}),
        )
