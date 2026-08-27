import json

import save_doubao_refs


def main():
    ok = save_doubao_refs.write_xlsx_from_csv()
    print(json.dumps({
        "ok": bool(ok),
        "xlsx": save_doubao_refs.OUT_XLSX if ok else "",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
