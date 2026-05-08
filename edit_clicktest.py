"""Make a copy of TestProject.ckp and insert a `C21 -> COIL(C22)` rung
into the Sub1 subroutine, ensuring RETURN remains the last rung.

Uses the ckp_decoder module's editing API exclusively -- this script
only orchestrates calls.
"""

import shutil
import sys
from ckp_decoder import decode_ckp


SRC = "demo/click_tests/TestProject.ckp"
DST = "demo/click_tests/TestProject.edited.ckp"


def main() -> int:
    # 1. Make a copy on disk first (so the original is untouched).
    shutil.copyfile(SRC, DST)
    print(f"copied {SRC} -> {DST}")

    # 2. Decode the copy.
    project = decode_ckp(open(DST, "rb").read())
    print(f"decoded variant {project.variant}, {len(project.subroutines)} subroutines")
    for sub in project.subroutines:
        print(f"  before: sub#{sub.sub_id} {sub.name!r} - {len(sub.rungs)} rungs (decoded)")

    # 3. Insert the new rung into Sub1 (sub_id = 2) before its RETURN.
    project.insert_no_out_rung_before_terminator(
        sub_id=2, tag_no="C21", tag_out="C22"
    )
    print("inserted rung: C21 -> COIL(C22) into Sub1 before RETURN")

    # 4. Save the modified file.
    project.save(DST)
    print(f"saved {DST}")

    # 5. Verify by re-decoding the saved file from disk.
    print("\nverifying...")
    new_project = decode_ckp(open(DST, "rb").read())
    print(new_project.render_program())
    return 0


if __name__ == "__main__":
    sys.exit(main())
