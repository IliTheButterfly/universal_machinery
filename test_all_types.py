"""Add a variety of rung types to TestProject.ckp using the new Rung API,
demonstrating that the tool can read AND write each instruction type."""
import shutil, struct, sys
from ckp_decoder import decode_ckp, compute_magic, Rung

SRC = "demo/click_tests/TestProject.ckp"
DST = "demo/click_tests/TestProject.all_types.ckp"

shutil.copyfile(SRC, DST)
project = decode_ckp(open(DST, "rb").read())

print(f"Starting from {SRC}")
print(f"Initial subroutines: {[s.name for s in project.subroutines]}")
print()

# 1. Add a NO + Out rung to Sub1: C40 -> COIL(C41)
project.add_rung(sub_id=2, rung=Rung.no_out("C40", "C41"))
print("Added Rung.no_out('C40', 'C41') to Sub1")

# 2. Add a 3-cell NO+NC+Out rung to Sub1: C50 AND NOT X005 -> COIL(C051)
project.add_rung(sub_id=2, rung=Rung.no_nc_out("C50", "X005", "C051"))
print("Added Rung.no_nc_out('C50', 'X005', 'C051') to Sub1")

# 3. Add a Copy rung to Sub1: Copy DS30 -> DS31
project.add_rung(sub_id=2, rung=Rung.copy("DS30", "DS31"))
print("Added Rung.copy('DS30', 'DS31') to Sub1")

# 4. Add a new subroutine 'Sub3' (cloned from Sub1) and a CALL to it from Main
new_id = project.add_subroutine_clone(source_sub_id=2, new_name="Sub3")
project.add_rung(sub_id=1, rung=Rung.call("Sub3"))
print(f"Added Sub3 (id={new_id}) and CALL Sub3 in Main")

project.save(DST)
print(f"\nSaved {DST}")

# Verify
new = decode_ckp(open(DST, "rb").read())
print("\nFinal program:")
print(new.render_program())

data = open(DST, "rb").read()
print(f"file: {len(data)} bytes")
print(f"magic stored = {struct.unpack('<H', data[:2])[0]:#06x}, "
      f"computed = {compute_magic(data):#06x}")
print(f"\nNicknames:")
for n in new.nicknames:
    print(f"  {n.address}")
