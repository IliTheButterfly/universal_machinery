"""Add a second rung to the already-edited TestProject.

Starts from TestProject.edited.ckp (which has C21 -> COIL(C22) inserted),
adds another rung X001 -> COIL(C30) before the RETURN, saves to a new
file, and reports back with a verification dump.
"""
import shutil
import struct
import sys
from click_plc import decode_ckp, compute_magic


SRC = "demo/click_tests/TestProject.edited.ckp"
DST = "demo/click_tests/TestProject.chained.ckp"


def main() -> int:
    shutil.copyfile(SRC, DST)
    project = decode_ckp(open(DST, "rb").read())

    print(f"Starting from: {SRC}")
    for sub in project.subroutines:
        print(f"  sub#{sub.sub_id} {sub.name!r}: {len(sub.rungs)} decoded rungs")

    # Add a second new rung to Sub1, before its RETURN.
    # Same API call -- only the tags change.
    project.insert_no_out_rung_before_terminator(
        sub_id=2, tag_no="C30", tag_out="C31"
    )
    print("\nInserted second rung: C30 -> COIL(C31) into Sub1 before RETURN")
    project.save(DST)
    print(f"Saved {DST}")

    # Verify
    new_project = decode_ckp(open(DST, "rb").read())
    print("\nFinal program:")
    print(new_project.render_program())

    data = open(DST, "rb").read()
    stored_magic = struct.unpack("<H", data[:2])[0]
    actual_magic = compute_magic(data)
    print(f"file size: {len(data)} bytes")
    print(f"magic stored = {stored_magic:#06x}, computed = {actual_magic:#06x}, "
          f"valid = {stored_magic == actual_magic}")

    # Quick check: verify SC-NICK got both new entries (C, 30 and C, 31)
    print(f"\nNicknames in SC-NICK ({len(new_project.nicknames)} entries):")
    for entry in new_project.nicknames:
        print(f"  {entry.address}  nickname={entry.nickname!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
