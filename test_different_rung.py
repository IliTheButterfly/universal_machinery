"""Make a fresh copy of TestProject.ckp and add a DIFFERENT kind of rung:
   X05 -> COIL(Y05)  (a physical input switching a physical output).
"""
import shutil, struct, sys
from ckp_decoder import decode_ckp, compute_magic

SRC = "demo/click_tests/TestProject.ckp"
DST = "demo/click_tests/TestProject.x05_y05.ckp"

shutil.copyfile(SRC, DST)
project = decode_ckp(open(DST, "rb").read())
print(f"Starting from {SRC}")
project.insert_no_out_rung_before_terminator(
    sub_id=2, tag_no="X05", tag_out="Y05"
)
project.save(DST)

new = decode_ckp(open(DST, "rb").read())
print("Final program:")
print(new.render_program())

data = open(DST, "rb").read()
print(f"file: {len(data)} bytes")
print(f"magic stored = {struct.unpack('<H', data[:2])[0]:#06x}, "
      f"computed = {compute_magic(data):#06x}")
print(f"nicknames: {[e.address for e in new.nicknames]}")
