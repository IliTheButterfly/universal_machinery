"""Test each rung type individually starting from TestProject.ckp."""
import shutil, struct, os
from ckp_decoder import decode_ckp, compute_magic, Rung

SRC = "demo/click_tests/TestProject.ckp"
TESTS = [
    ("just_no_out",     lambda p: p.add_rung(2, Rung.no_out("C40", "C41"))),
    ("just_no_nc_out",  lambda p: p.add_rung(2, Rung.no_nc_out("C50", "X005", "C051"))),
    ("just_copy",       lambda p: p.add_rung(2, Rung.copy("DS30", "DS31"))),
    ("just_clone_sub",  lambda p: p.add_subroutine_clone(2, "Sub3")),
    ("just_call",       lambda p: (p.add_subroutine_clone(2, "Sub3"),
                                     p.add_rung(1, Rung.call("Sub3")))),
]

WINE = os.path.expanduser("~/.wine_click/drive_c")

for name, op in TESTS:
    dst = f"demo/click_tests/TestProject.{name}.ckp"
    shutil.copyfile(SRC, dst)
    p = decode_ckp(open(dst, "rb").read())
    op(p)
    p.save(dst)
    data = open(dst, "rb").read()
    valid = struct.unpack('<H', data[:2])[0] == compute_magic(data)
    print(f"  {name:20s}  size={len(data):>6}  magic-valid={valid}")
    # Stage to wine
    shutil.copyfile(dst, f"{WINE}/TestProject_{name}.ckp")

print(f"\nAll staged in {WINE}/")
print("Please test each one in CLICK and tell me which open and which fail")
