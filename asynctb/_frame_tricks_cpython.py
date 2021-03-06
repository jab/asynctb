import ctypes
import dis
import gc
import sys
from types import FrameType
from typing import List, Tuple, Type
from ._frames import FrameDetails

def inspect_frame(frame: FrameType) -> FrameDetails:
    assert sys.implementation.name == "cpython"

    details = FrameDetails()
    wordsize = ctypes.sizeof(ctypes.c_ulong)

    # This is the layout of the start of a frame object. It has a couple
    # fields we can't access from Python, especially f_valuestack and
    # f_stacktop.
    class FrameObjectStart(ctypes.Structure):
        _fields_: List[Tuple[str, Type[ctypes._CData]]] = [
            ("ob_refcnt", ctypes.c_ulong),  # reference count
            ("ob_type", ctypes.c_ulong),  # PyTypeObject*
            ("ob_size", ctypes.c_ulong),  # number of pointers after f_localsplus
            ("f_back", ctypes.c_ulong),  # PyFrameObject*
            ("f_code", ctypes.c_ulong),  # PyCodeObject*
            ("f_builtins", ctypes.c_ulong),  # PyDictObject*
            ("f_globals", ctypes.c_ulong),  # PyDictObject*
            ("f_locals", ctypes.c_ulong),  # PyObject*, some mapping
            ("f_valuestack", ctypes.c_ulong),  # PyObject**, points within self
            ("f_stacktop", ctypes.c_ulong),  # PyObject**, points within self
            # and then we start seeing differences between different
            # Python versions
        ]
        extra_header_bytes = object().__sizeof__() - 2 * wordsize
        if extra_header_bytes:
            _fields_.insert(0, ("ob_debug", ctypes.c_byte * extra_header_bytes))

    # Basic sanity checks cross-referencing the values we can get from Python
    # with their Python values
    frame_raw = FrameObjectStart.from_address(id(frame))
    refcnt = frame_raw.ob_refcnt
    assert refcnt + 1 == sys.getrefcount(frame)
    assert frame_raw.ob_type == id(type(frame))
    assert frame_raw.f_back == (id(frame.f_back) if frame.f_back is not None else 0)
    assert frame_raw.f_code == id(frame.f_code)
    assert frame_raw.f_globals == id(frame.f_globals)

    # The frame object has a fixed-length part followed by f_localsplus
    # which is a variable-length array of PyObject*. The array contains
    # co_nlocals + len(co_cellvars) + len(co_freevars) slots for
    # those things, followed by co_stacksize slots for the bytecode
    # interpreter stack. f_valuestack points at the beginning of the
    # stack part. Figure out where f_localsplus is. (It's a constant
    # offset from the start of the frame, but the constant differs
    # by Python version.)
    co = frame.f_code
    stack_start_offset = frame_raw.f_valuestack - id(frame)
    localsplus_offset = stack_start_offset - wordsize * (
        co.co_nlocals + len(co.co_cellvars) + len(co.co_freevars)
    )
    end_offset = stack_start_offset + wordsize * co.co_stacksize

    # Make sure our inferred size for the overall frame object matches
    # what Python says and what the ob_size field says. Note ob_size can
    # be larger than necessary due to frame object reuse.
    assert frame_raw.ob_size >= (end_offset - localsplus_offset) / wordsize
    assert end_offset == frame.__sizeof__()

    # Figure out what portion of the stack is actually valid, and extract
    # the PyObject pointers. We just store their addresses (id), not taking
    # references or anything.
    if frame_raw.f_stacktop == 0:
        # Frames that are currently executing have a NULL stacktop.
        # Copy the whole stack; we'll trim it below based on which blocks
        # are active.
        stack_top_offset = end_offset
    else:
        stack_top_offset = frame_raw.f_stacktop - id(frame)
        assert stack_start_offset <= stack_top_offset <= end_offset
    stack = [
        ctypes.c_ulong.from_address(id(frame) + offset).value
        for offset in range(stack_start_offset, stack_top_offset, wordsize)
    ]
    # Now stack[i] corresponds to f_valuestack[i] in C.

    # Figure out the active context managers. Each context manager
    # pushes a block to a fixed-size block stack (20 12-byte entries,
    # this has been unchanged for ages) which is stored by value right
    # before f_localsplus. There's another frame field for the size of
    # the block stack.
    class PyTryBlock(ctypes.Structure):
        _fields_ = [
            # An opcode; the blocks we want are SETUP_FINALLY
            ("b_type", ctypes.c_int),
            # An offset in co.co_code; the blocks we want have a
            # WITH_CLEANUP_START opcode at this offset
            ("b_handler", ctypes.c_int),
            # An index on the value stack; if we're still in the body
            # of the with statement, the blocks we want have
            # an __exit__ or __aexit__ method at stack index b_level - 1
            ("b_level", ctypes.c_int),
        ]

    blockstack_offset = localsplus_offset - 20 * ctypes.sizeof(PyTryBlock)
    f_iblock = ctypes.c_int.from_address(id(frame) + blockstack_offset - 8)
    f_lasti = ctypes.c_int.from_address(id(frame) + blockstack_offset - 16)
    assert f_lasti.value == frame.f_lasti
    assert 0 <= f_iblock.value <= 20
    assert blockstack_offset > ctypes.sizeof(FrameObjectStart)

    blockstack_end_offset = blockstack_offset + (
        f_iblock.value * ctypes.sizeof(PyTryBlock)
    )
    assert blockstack_offset <= blockstack_end_offset <= localsplus_offset

    # Process blocks on the current block stack
    while blockstack_offset < blockstack_end_offset:
        block = PyTryBlock.from_address(id(frame) + blockstack_offset)
        assert (
            0 < block.b_type <= 257
            and (
                # EXCEPT_HANDLER blocks (type 257) have a bogus b_handler
                block.b_handler == -1
                if block.b_type == 257
                else 0 < block.b_handler < len(co.co_code)
            )
            and 0 <= block.b_level <= len(stack)
        )

        # Looks like a valid block -- is it one of our context managers?
        if (
            block.b_type == dis.opmap["SETUP_FINALLY"]
            and co.co_code[block.b_handler] == dis.opmap["WITH_CLEANUP_START"]
        ):
            # Yup. Still fully inside the block; use b_level to find the
            # __exit__ or __aexit__ method
            details.blocks.append(
                FrameDetails.FinallyBlock(handler=block.b_handler, level=block.b_level)
            )

        blockstack_offset += ctypes.sizeof(PyTryBlock)

    # Map the addresses in `stack` back to actual objects. The safest
    # way is to use gc.get_referents(). For an executing frame, though,
    # get_referents() doesn't walk the value stack, so we have to make our
    # references the hard way.
    if frame_raw.f_stacktop == 0:
        stack_validity_limit = max(blk.level for blk in details.blocks)
        del stack[stack_validity_limit :]
        details.stack = [
            None
            if address == 0
            # Needs ignore till https://github.com/python/typeshed/pull/4311 is released
            else ctypes.cast(address, ctypes.py_object).value  # type: ignore
            for address in stack
        ]
    else:
        object_from_id_map = {id(obj): obj for obj in gc.get_referents(frame)}
        details.stack = [object_from_id_map.get(value) for value in stack]

    return details
