import sys
import struct

def extract_utf16le_strings(data):
    """Scan binary data for UTF-16LE strings longer than 2 characters."""
    strings = {}
    i = 0
    while i < len(data) - 2:
        # Try to decode at this offset
        try:
            end = i
            chars = []
            while end + 1 < len(data):
                c = data[end:end+2]
                if c == b'\x00\x00':
                    break
                chars.append(c)
                end += 2
            if len(chars) >= 2:  # min length 2 chars
                s = b''.join(chars).decode('utf-16le', errors='ignore')
                strings[i] = s
            i = end + 2
        except:
            i += 2
    return strings


def is_nc(word):
    """Return True if contact is normally-closed."""
    return (word & 0x01) != 0


def parse_tmp_file(filename):
    with open(filename, "rb") as f:
        data = f.read()

    string_table = extract_utf16le_strings(data)

    offset = 0
    rungs = []
    current_contacts = []
    coil = None

    while offset < len(data) - 2:
        word_val = struct.unpack_from("<H", data, offset)[0]

        # Detect ContactNO (from previous dumps)
        if word_val == 0x1100 and offset + 4 <= len(data):
            tag_offset = struct.unpack_from("<H", data, offset + 2)[0]
            tag = string_table.get(tag_offset, f"Tag{tag_offset}")
            if offset + 6 <= len(data):
                nc_flag = struct.unpack_from("<H", data, offset + 4)[0]
                nc = is_nc(nc_flag)
                current_contacts.append((tag, nc))
            offset += 6
            continue

        # Detect Out / Coil (from previous dumps)
        elif word_val == 0x1500 and offset + 4 <= len(data):
            coil_offset = struct.unpack_from("<H", data, offset + 2)[0]
            coil = string_table.get(coil_offset, f"Tag{coil_offset}")
            # Build rung expression
            expr = []
            for tag, nc in current_contacts:
                if nc:
                    expr.append(f"NOT {tag}")
                else:
                    expr.append(tag)
            rung_expr = " AND ".join(expr)
            rungs.append(f"{rung_expr} -> Coil({coil})")
            current_contacts = []
            coil = None
            offset += 4
            continue

        # Detect Return instruction
        elif word_val == 0xd501:  # from your dump
            rungs.append("Return()")
            offset += 2
            continue

        else:
            offset += 2

    return rungs


def main(v):
    rungs = parse_tmp_file(v[1])
    for i, rung in enumerate(rungs, 1):
        print(f"Rung {i}: {rung}")

if __name__ == "__main__":
    main(sys.argv)
