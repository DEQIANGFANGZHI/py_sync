# -*- coding: utf-8 -*-
"""
test_doc.py —— 从「智能表格」读取数据，写入「智能文档」

目标对象（已实测通过）：
  智能表格 dc4cm_KrUw...  子表 thaLFC（标题 test）
  智能文档 dcvso75vjZm... 页面 1jigjo（标题 test）

流程：
  1. 获取 access_token（★ 这两个文档都属于 yushou 应用）
  2. smartsheet/get_records 分页读取智能表格记录
  3. smartdoc/get_block_list + delete_blocks 清空页面旧内容（CLEAR_FIRST 控制）
  4. smartdoc/add_blocks 分批写入（标题 + 说明 + 每行一条）
  5. 把 docid / page_id / block_id 落盘到 智能文档缓存.json

只依赖 requests，直接 `python test_doc.py` 运行。
"""

import json
import os
import time
from datetime import datetime

import requests

# ============================================================
# 一、企业微信凭证
# ============================================================
# ★ 实测：智能表格 dc4cm_ 与智能文档 dcvso7 都属于 yushou 应用，
#   换成 main 应用的 secret 会报 701450 no privilege to access this file or doc
CORPID = 'ww72152ae3c89a4de5'
SECRET = 'XoiFjAH6HbViugpVa0zSY7bARxGKhZ-evO3fQeOKJfU'

# ============================================================
# 二、数据来源：智能表格（子表 thaLFC，标题 test）
# ============================================================
SRC_DOCID = 'dc4cm_KrUwKuCEN1bWkoxD_4Z8vRjrh44dT2CrMIMER1PLe1f_syfN-OSVOZiRCOZ2dQCOy_3ItiUp5tmK0ItiWA'
SRC_SHEET_ID = 'thaLFC'

# 要读取的列（get_fields 实测：文本 / 文本 2 / 人员 / 数字 / 最后编辑时间）
# 人员字段结构特殊（FIELD_TYPE_USER），先跳过
FIELDS = ['文本', '文本 2', '数字', '最后编辑时间']

# ============================================================
# 三、目标：智能文档（page_id=1jigjo，标题 test）
# ============================================================
DOC_NAME = '智能表格数据测试文档'
ADMIN_USERS = ['LiJiaFeng']

SMART_DOC_ID = 'dcvso75vjZmJMhaaaoEucJ9cGHMbeEegfSNxhPengI7PqpzWpYkHcdsrsOF-YNe05aqY9kYZnN0NKjHP8vw9q-Aw'
PAGE_ID = '1jigjo'   # get_page_hierarchy 实测得到；留空则自动取根页面

# ============================================================
# 四、运行参数
# ============================================================
MAX_RECORDS = 20       # 最多写多少条；0 = 全部
BLOCK_BATCH = 20       # 单次 add_blocks 的块数量
DELETE_BATCH = 100     # 单次 delete_blocks 的 ID 数量
CLEAR_FIRST = True     # 写入前先清空页面已有内容，避免重复运行堆积

# docid / page_id / block_id 缓存文件（docid 只在创建时返回一次，必须落盘）
DOC_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "智能文档缓存.json")

# ============================================================
# 五、写入规则（想改文档呈现效果，只改这一段即可）
# ============================================================
DOC_HEADER = "智能表格数据快照"   # 一级标题文案

# 这些列的值是时间戳，写入前转成可读时间（其它列原样输出）
TIME_FIELDS = {'最后编辑时间', '创建时间'}

# 系统字段不会出现在 get_records 的 values 里，只能从记录顶层元信息取
# （实测顶层只有 create_time / update_time / creator_name / updater_name）
RECORD_META = {
    '最后编辑时间': 'update_time',
    '创建时间': 'create_time',
    '最后编辑人': 'updater_name',
    '创建人': 'creator_name',
}

# 每条记录一行的模板：{i}=序号；花括号里直接写智能表格的列名
# 例："{i}. {文本} / {文本 2}（{数字}）"
LINE_TEMPLATE = "{i}. 文本：{文本}，文本 2：{文本 2}，数字：{数字}，最后编辑时间：{最后编辑时间}"


# ============================================================
# token：过期自动重取
# ============================================================
token_info = {}


def get_token():
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORPID}&corpsecret={SECRET}"
    resp = requests.get(url, timeout=30).json()
    if resp.get("errcode") == 0:
        return {"token": resp["access_token"], "expires_in": resp.get("expires_in", 7200)}
    print(f"获取token失败: {resp}")
    return None


def get_valid_token():
    global token_info
    if not token_info or time.time() >= token_info.get('expiry_time', 0):
        new_token = get_token()
        if new_token is None:
            return ""
        token_info = {
            "token": new_token["token"],
            "expiry_time": time.time() + new_token["expires_in"] - 60
        }
    return token_info["token"]


# ============================================================
# 1. 读取智能表格记录
# ============================================================
def read_sheet_records(access_token):
    """分页拉取智能表格记录"""
    all_records = []
    offset = 0
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/get_records?access_token={access_token}"
    while True:
        data = {
            "docid": SRC_DOCID,
            "sheet_id": SRC_SHEET_ID,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "field_titles": FIELDS,
            "offset": offset,
        }
        resp = requests.post(url=url, json=data, timeout=30).json()
        if resp.get("errcode") != 0:
            print(f"读取记录失败: {resp}")
            break

        all_records.extend(resp.get("records", []))

        if MAX_RECORDS and len(all_records) >= MAX_RECORDS:
            all_records = all_records[:MAX_RECORDS]
            break
        if not resp.get("has_more"):
            break
        offset = resp.get("next", 0)

    print(f"读取智能表格记录，共 {len(all_records)} 条")
    return all_records


def extract_field_value(record, field_name):
    """兼容文本格式 [{"type":"text","text":"..."}] 和直接赋值（数字/时间）

    注意：最后编辑时间/创建时间/创建人/最后编辑人 是系统字段，
    get_records 不会放进 values，只能取记录顶层的 update_time 等字段。
    """
    meta_key = RECORD_META.get(field_name)
    if meta_key:
        return record.get(meta_key)

    cell = record.get("values", {}).get(field_name)
    if cell is None:
        return None
    if isinstance(cell, list):
        return cell[0].get("text", "") if cell else ""
    return cell


def fmt_value(value, field=None):
    """数字去掉多余小数；人员等 dict 取可读字段；时间戳列转可读时间；None 转空串"""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or value.get("userid") or value)
    text = str(value)
    if field in TIME_FIELDS and text.isdigit():
        ts = int(text)
        if ts < 10**12:          # 秒级时间戳 -> 毫秒
            ts *= 1000
        return datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return text


# ============================================================
# 2. 创建 / 复用智能文档
# ============================================================
def create_smart_doc(access_token):
    """doc_type=11 -> 智能文档；3=文档，10=智能表格"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/create_doc?access_token={access_token}"
    data = {
        "doc_type": 11,
        "doc_name": DOC_NAME,
        "admin_users": ADMIN_USERS,
    }
    resp = requests.post(url=url, json=data, timeout=30).json()
    print(f"创建智能文档: {resp}")
    if resp.get("errcode") != 0:
        return None
    return {"docid": resp.get("docid"), "url": resp.get("url")}


def load_cache():
    if os.path.exists(DOC_CACHE_FILE):
        with open(DOC_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_cache(**kwargs):
    """把 docid / url / page_id / blocks 合并写入缓存"""
    cache = load_cache()
    cache.update(kwargs)
    with open(DOC_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"已写入缓存 {DOC_CACHE_FILE}")


def resolve_doc(access_token):
    """优先用手工指定的 -> 缓存里的 -> 新建"""
    if SMART_DOC_ID:
        print(f"使用配置里指定的智能文档: {SMART_DOC_ID}")
        return SMART_DOC_ID

    cache = load_cache()
    if cache.get("docid"):
        print(f"复用已缓存的智能文档: {cache['docid']}")
        return cache["docid"]

    info = create_smart_doc(access_token)
    if not info or not info.get("docid"):
        print("创建智能文档失败，终止")
        return None
    save_cache(docid=info["docid"], url=info.get("url"))
    print(f"智能文档访问链接: {info.get('url')}")
    return info["docid"]


# ============================================================
# 3. 取页面 page_id
# ============================================================
def get_root_page_id(access_token, docid):
    """取根页面 page_id。实测 parent_id 可能为空，也可能等于 page_id 自身"""
    if PAGE_ID:
        print(f"使用配置里指定的 page_id: {PAGE_ID}")
        return PAGE_ID

    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartdoc/get_page_hierarchy?access_token={access_token}"
    resp = requests.post(url=url, json={"docid": docid}, timeout=30).json()
    if resp.get("errcode") != 0:
        print(f"获取页面结构失败: {resp}")
        return None

    pages = resp.get("pages", [])
    if not pages:
        print("该智能文档没有任何页面")
        return None
    for p in pages:
        print(f"  页面: page_id={p.get('page_id')} title={p.get('title')} parent_id={p.get('parent_id') or '(根)'}")
    for p in pages:
        pid = p.get("page_id")
        parent = p.get("parent_id")
        if not parent or parent == pid:
            return pid
    return pages[0].get("page_id")


# ============================================================
# 4. 清空页面已有内容
# ============================================================
def list_all_blocks(access_token, docid, page_id):
    """分页列出页面里所有块的 id（has_more 返回的是字符串 "true"/"false"）"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartdoc/get_block_list?access_token={access_token}"
    ids = []
    start = 0
    while True:
        resp = requests.post(
            url=url,
            json={"docid": docid, "page_id": page_id, "start": start, "limit": 200},
            timeout=30,
        ).json()
        if resp.get("errcode") != 0:
            print(f"获取内容块列表失败: {resp}")
            break

        ids.extend([b.get("id") for b in resp.get("blocks", []) if b.get("id")])

        if str(resp.get("has_more")).lower() != "true":
            break
        next_start = resp.get("next_start")
        if not next_start:
            break
        start = next_start

    print(f"页面现有内容块 {len(ids)} 个")
    return ids


def delete_blocks(access_token, docid, page_id, ids):
    """分批删除块"""
    if not ids:
        print("没有需要删除的内容块")
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartdoc/delete_blocks?access_token={access_token}"
    for i in range(0, len(ids), DELETE_BATCH):
        batch = ids[i:i + DELETE_BATCH]
        resp = requests.post(
            url=url,
            json={"docid": docid, "page_id": page_id, "ids": batch},
            timeout=30,
        ).json()
        print(f"删除 {i + 1}-{i + len(batch)}/{len(ids)}: {resp}")
        if resp.get("errcode") != 0:
            break


def clear_page(access_token, docid, page_id):
    if not CLEAR_FIRST:
        print("CLEAR_FIRST=False，跳过清空，内容将追加在已有内容后面")
        return
    ids = list_all_blocks(access_token, docid, page_id)
    delete_blocks(access_token, docid, page_id, ids)


# ============================================================
# 5. 写入内容块
# ============================================================
class _SafeDict(dict[str, str]):
    """模板里出现没提供的列名时输出空串，而不是报错"""
    def __missing__(self, key: str) -> str:
        return ""


def build_blocks(records):
    """按写入规则配置，把表格记录拼成内容块列表"""
    blocks = [
        {"type": "BLOCK_TYPE_HEADER_1", "title": DOC_HEADER},
        {
            "type": "BLOCK_TYPE_TEXT",
            "title": f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}，共 {len(records)} 条记录",
        },
        {"type": "BLOCK_TYPE_DIVIDER", "title": ""},
    ]

    for i, rec in enumerate(records, 1):
        vals = {f: fmt_value(extract_field_value(rec, f), f) for f in FIELDS}
        vals["i"] = str(i)
        blocks.append({
            "type": "BLOCK_TYPE_TEXT",
            "title": LINE_TEMPLATE.format_map(_SafeDict(vals)),
        })

    return blocks


def add_blocks(access_token, docid, page_id, blocks):
    """分批写入内容块。

    实测：不带 after_id 时，服务端把每个块依次插入到页面最前面，
    数组越靠后显示越靠上。所以先把整列表反转再发，最终页面顺序
    才与 blocks 参数一致（标题在最上、记录按 1..N 排列）。
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartdoc/add_blocks?access_token={access_token}"
    added_ids = []
    send = list(reversed(blocks))

    for i in range(0, len(send), BLOCK_BATCH):
        batch = send[i:i + BLOCK_BATCH]

        resp = requests.post(
            url=url,
            json={"docid": docid, "page_id": page_id, "blocks": batch},
            timeout=30,
        ).json()

        if resp.get("errcode") != 0:
            print(f"写入第 {i + 1}-{i + len(batch)} 块失败: {resp}")
            continue

        ids = [b.get("id") for b in resp.get("blocks", []) if b.get("id")]
        added_ids.extend(ids)
        print(f"写入 {i + 1}-{i + len(batch)}/{len(send)} 块成功，返回 {len(ids)} 个 block_id")

    print(f"写入完成，共 {len(added_ids)} 个块")
    return added_ids


# ============================================================
# 主流程
# ============================================================
def main():
    token = get_valid_token()
    if not token:
        print("获取 access_token 失败")
        return

    records = read_sheet_records(token)
    if not records:
        print("没有读到数据，终止")
        return

    print("前 3 条样例：")
    for rec in records[:3]:
        print("  ", {f: extract_field_value(rec, f) for f in FIELDS})

    docid = resolve_doc(token)
    if not docid:
        return

    page_id = get_root_page_id(token, docid)
    if not page_id:
        return
    print(f"使用 page_id: {page_id}")

    clear_page(token, docid, page_id)

    blocks = build_blocks(records)
    added_ids = add_blocks(token, docid, page_id, blocks)

    save_cache(docid=docid, page_id=page_id, blocks=added_ids)
    print(f"完成，智能文档 docid: {docid}  page_id: {page_id}  block 数: {len(added_ids)}")


if __name__ == "__main__":
    main()
