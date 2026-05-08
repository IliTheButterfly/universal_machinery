# save_rcdata.py
import pefile, sys

pe = pefile.PE(sys.argv[1])
i = 0
for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
    # Look for RT_RCDATA (id 10) or scan all
    if entry.name is not None or entry.struct.Id in (10,):
        for res in entry.directory.entries:
            data_rva = res.directory.entries[0].data.struct.OffsetToData
            size = res.directory.entries[0].data.struct.Size
            data = pe.get_memory_mapped_image()[data_rva:data_rva+size]
            fn = f"demo/resource_{i}.bin"
            open(fn, "wb").write(data)
            print("wrote", fn)
            i += 1
