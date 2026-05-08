"""Add a new subroutine 'Sub3' (cloned from Sub1) and call it from Main."""
import shutil, struct, sys
from click_plc import decode_ckp, compute_magic

SRC = "demo/click_tests/TestProject.ckp"
DST = "demo/click_tests/TestProject.with_sub3.ckp"

shutil.copyfile(SRC, DST)
project = decode_ckp(open(DST, "rb").read())

print(f"Starting from {SRC}")
print(f"Initial subroutines:")
for s in project.subroutines:
    print(f"  sub#{s.sub_id} {s.name!r}")

# Add a new subroutine "Sub3" cloned from Sub1 (sub_id=2, 4-char name)
new_id = project.add_subroutine_clone(source_sub_id=2, new_name="Sub3")
print(f"\nAdded new subroutine: sub_id={new_id}, name='Sub3'")

# Call it from Main Program (sub_id=1)
project.insert_call_rung_before_terminator(caller_sub_id=1, target_sub_name="Sub3")
print(f"Inserted CALL Sub3 in Main Program before End")

project.save(DST)
print(f"Saved {DST}")

# Reload and dump
new = decode_ckp(open(DST, "rb").read())
print("\nFinal program:")
print(new.render_program())

data = open(DST, "rb").read()
print(f"file: {len(data)} bytes  (orig was {len(open(SRC, 'rb').read())})")
print(f"magic stored = {struct.unpack('<H', data[:2])[0]:#06x}, "
      f"computed = {compute_magic(data):#06x}")
print(f"sub_count = {struct.unpack_from('<H', data, 0x2a)[0]}")
