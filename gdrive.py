"""從 Google 雲端抓資料：工作單（Google 試算表）與布標資料夾（.ezp）。

兩者都必須設成「知道連結的任何人都可以檢視」，程式不登入 Google 帳號：
- 試算表：用匯出網址下載成 xlsx。
- 資料夾：用雲端硬碟的內嵌清單頁（embeddedfolderview）列出檔案，再逐一下載。
  布標下載後存在本機 ezp/品牌/檔名.ezp，列印時讀本機檔，斷網也能印。
"""
import html
import json
import os
import re
import urllib.error
import urllib.request

import settings

TIMEOUT = 30
EZP_DIR = os.path.join(settings.BASE_DIR, "ezp")
MANIFEST_PATH = os.path.join(EZP_DIR, "manifest.json")

_ENTRY = re.compile(
    r'<div class="flip-entry" id="entry-([\w-]+)".*?'
    r'<a href="([^"]*)".*?'
    r'<div class="flip-entry-title">(.*?)</div>.*?'
    r'<div class="flip-entry-last-modified"><div>(.*?)</div>',
    re.S,
)


class SourceError(Exception):
    """抓不到雲端資料（網址不對、沒開放檢視、斷網）。訊息可直接顯示給使用者。"""


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AutoLogo"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise SourceError("Google 拒絕存取。請確認共用設定是「知道連結的任何人都可以檢視」") from None
        raise SourceError(f"Google 回應錯誤 {e.code}") from None
    except OSError as e:
        raise SourceError(f"連不上 Google（{e}）。請檢查網路") from None


def sheet_id(url):
    m = re.search(r"/spreadsheets/d/([\w-]+)", url or "")
    if not m:
        raise SourceError("工作單網址不正確，請到「設定」貼上 Google 試算表的網址")
    return m.group(1)


def folder_id(url):
    m = re.search(r"/folders/([\w-]+)", url or "") or re.search(r"[?&]id=([\w-]+)", url or "")
    if not m:
        raise SourceError("布標資料夾網址不正確，請到「設定」貼上 Google 雲端硬碟資料夾的網址")
    return m.group(1)


def fetch_sheet(url):
    """下載整份試算表（所有工作表）的 xlsx 內容。"""
    data, ctype = _get(f"https://docs.google.com/spreadsheets/d/{sheet_id(url)}/export?format=xlsx")
    if "spreadsheetml" not in ctype:  # 沒開放檢視時 Google 會回登入頁（HTML）
        raise SourceError("無法下載工作單。請確認試算表的共用設定是「知道連結的任何人都可以檢視」")
    return data


def list_folder(fid):
    """回傳 [{"id", "name", "folder", "modified"}]。"""
    page, _ = _get(f"https://drive.google.com/embeddedfolderview?id={fid}#list")
    text = page.decode("utf-8", "replace")
    if "flip-entries" not in text and "flip-entry" not in text:
        raise SourceError("無法讀取布標資料夾。請確認資料夾的共用設定是「知道連結的任何人都可以檢視」")
    return [
        {"id": i, "name": html.unescape(name).strip(), "folder": "/folders/" in href, "modified": html.unescape(mod)}
        for i, href, name, mod in _ENTRY.findall(text)
    ]


def download(file_id):
    data, ctype = _get(f"https://drive.google.com/uc?export=download&id={file_id}")
    if "text/html" in ctype:
        raise SourceError("下載布標檔失敗（Google 回傳網頁而不是檔案）")
    return data


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "_"


def load_manifest():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sync(folder_url):
    """把雲端資料夾的 .ezp 同步到本機 ezp/。資料夾第一層是品牌，品牌底下是各型號的 .ezp。
    只下載新增或修改過的檔案；雲端已刪除的檔案本機也刪除。回傳摘要文字。"""
    root = folder_id(folder_url)
    old = load_manifest()
    new, downloaded, failed = {}, 0, []
    for brand in list_folder(root):
        if not brand["folder"]:
            continue
        for f in list_folder(brand["id"]):
            if f["folder"] or not f["name"].lower().endswith(".ezp"):
                continue
            rel = os.path.join(_safe(brand["name"]), _safe(f["name"]))
            path = os.path.join(EZP_DIR, rel)
            prev = old.get(f["id"])
            entry = {"brand": brand["name"], "name": f["name"], "path": rel, "modified": f["modified"]}
            if not (prev and prev.get("modified") == f["modified"] and prev.get("path") == rel and os.path.exists(path)):
                try:
                    data = download(f["id"])
                except SourceError as e:
                    failed.append(f'{brand["name"]}/{f["name"]}（{e}）')
                    if prev and os.path.exists(os.path.join(EZP_DIR, prev.get("path", ""))):
                        new[f["id"]] = prev  # 保留舊版，下次再試
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as out:
                    out.write(data)
                downloaded += 1
            new[f["id"]] = entry
    keep = {e["path"] for e in new.values()}
    removed = 0
    for e in old.values():
        if e.get("path") and e["path"] not in keep:
            try:
                os.remove(os.path.join(EZP_DIR, e["path"]))
                removed += 1
            except OSError:
                pass
    os.makedirs(EZP_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    msg = f"布標共 {len(new)} 個，這次下載 {downloaded} 個"
    if removed:
        msg += f"、移除 {removed} 個"
    if failed:
        msg += f"；{len(failed)} 個下載失敗：" + "、".join(failed[:5])
    return msg


def library():
    """本機布標清單：[{"brand", "name", "path"(絕對路徑)}]。
    直接掃描 ezp/ 資料夾，所以手動放進 ezp/品牌/ 的檔案也算。"""
    out = []
    if not os.path.isdir(EZP_DIR):
        return out
    for brand in sorted(os.listdir(EZP_DIR)):
        bdir = os.path.join(EZP_DIR, brand)
        if not os.path.isdir(bdir):
            continue
        for name in sorted(os.listdir(bdir)):
            if name.lower().endswith(".ezp"):
                out.append({"brand": brand, "name": name, "path": os.path.join(bdir, name)})
    return out
