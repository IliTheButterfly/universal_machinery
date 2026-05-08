"""Chain a second rung onto the working X05_Y05 file."""
import shutil, struct, sys
from click_plc import decode_ckp, compute_magic

SRC = "demo/click_tests/TestProject.x05_y05.ckp"
DST = "demo/click_tests/TestProject.x05_y05_plus.ckp"

shutil.copyfile(SRC, DST)
project = decode_ckp(open(DST, "rb").read())
print(f"Starting from {SRC}")
project.insert_no_out_rung_before_terminator(
    sub_id=2, tag_no="X10", tag_out="Y10"
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
