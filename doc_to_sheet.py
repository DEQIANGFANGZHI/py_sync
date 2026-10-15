# -*- coding: utf-8 -*-
"""
doc_to_sheet.py —— 反向同步：从「智能文档」解析数据，写入「智能表格」

对象：
  智能文档 dcvso75vjZm... 页面 1jigjo（test_doc.py 写进去的那些行）
  智能表格 dc4cm_KrUw...  子表 thaLFC（标题 test）

流程：
  1. 获取 access_token（★ 用 yushou 应用的 secret）
  2. smartdoc/get_block_list 读取智能文档页面的所有内容块
  3. 过滤掉 标题块 / 生成时间 / 分隔线，剩下的按序号解析成一行数据
  4. 取第 N 条（TARGET_INDEX，从 1 开始）
  5. smartsheet/get_fields 拿目标表格字段类型
  6. smartsheet/add_records 写入（按字段类型组装 value）

只依赖 requests，直接 `python doc_to_sheet.py` 运行。
"""

import json
import os
import re
import time
from datetime import datetime

import requests

# ============================================================
# 一、企业微信凭证（同 test_doc.py：yushou 应用）
# ============================================================
CORPID = 'ww72152ae3c89a4de5'
SECRET = 'XoiFjAH6HbViugpVa0zSY7bARxGKhZ-evO3fQeOKJfU'

# ============================================================
# 二、来源：智能文档
# ============================================================
SMART_DOC_ID = 'dcvso75vjZmJMhaaaoEucJ9cGHMbeEegfSNxhPengI7PqpzWpYkHcdsrsOF-YNe05aqY9kYZnN0NKjHP8vw9q-Aw'
PAGE_ID = '1jigjo'

# ============================================================
# 三、目标：智能表格
# ============================================================
DST_DOCID = 'dc4cm_KrUwKuCEN1bWkoxD_4Z8vRjrh44dT2CrMIMER1PLe1f_syfN-OSVOZiRCOZ2dQCOy_3ItiUp5tmK0ItiWA'
DST_SHEET_ID = 'thaLFC'

# ============================================================
# 四、运行参数
# ============================================================
TARGET_INDEX = 2          # 取智能文档里的第几条记录（1 开始）
DRY_RUN = False           # True = 只预览解析结果和请求包体，不真正写入
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "智能表格写入缓存.json")

# 系统字段无法通过接口写入，自动跳过
SKIP_FIELD_TYPES = {
    'FIELD_TYPE_CREATED_TIME',
    'FIELD_TYPE_MODIFIED_TIME',
    'FIELD_TYPE_CREATED_USER',
    'FIELD_TYPE_MODIFIED_USER',
}


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
# 1. 读取智能文档的内容块
# ============================================================
def block_text(block):
    """取块的文本内容，兼容 title / text 两种返回结构"""
    if block.get("title"):
        return block["title"]
    text = block.get("text")
    if isinstance(text, dict):
        return text.get("text", "")
    if isinstance(text, list):
        return "".join(t.get("text", "") if isinstance(t, dict) else str(t) for t in text)
    return text or ""


def list_blocks(access_token, docid, page_id):
    """分页读取页面所有块"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartdoc/get_block_list?access_token={access_token}"
    blocks = []
    start = 0
    while True:
        resp = requests.post(
            url=url,
            json={"docid": docid, "page_id": page_id, "start": start, "limit": 200},
            timeout=30,
        ).json()
        if resp.get("errcode") != 0:
            print(f"获取内容块失败: {resp}")
            break
        blocks.extend(resp.get("blocks", []))
        if str(resp.get("has_more")).lower() != "true":
            break
        next_start = resp.get("next_start")
        if not next_start:
            break
        start = next_start

    print(f"页面共读取到 {len(blocks)} 个块")
    return blocks


def filter_record_lines(blocks):
    """去掉标题块 / 生成时间 / 分隔线，只留记录行（以 '数字. ' 开头）"""
    lines = []
    for b in blocks:
        btype = b.get("block_type") or b.get("type") or ""
        text = block_text(b).strip()
        if 'HEADER' in str(btype) or 'DIVIDER' in str(btype):
            continue
        if not text or text.startswith("生成时间："):
            continue
        if not re.match(r"^\d+[\.、]", text):
            continue
        lines.append(text)
    return lines


def parse_line(line, field_titles):
    """'1. 文本：aaa，文本 2：bbb' -> {'文本': 'aaa', '文本 2': 'bbb'}"""
    line = re.sub(r"^\d+[\.、]\s*", "", line)
    result = {}
    # 按 “字段名：值” 切分，字段名取已知列里最长的那个做匹配
    pattern = "|".join(re.escape(f) for f in sorted(field_titles, key=len, reverse=True))
    for m in re.finditer(rf"({pattern})\s*[：:]\s*(.*?)(?=\s*，\s*(?:{pattern})\s*[：:]|$)", line):
        result[m.group(1)] = m.group(2).strip()
    return result


# ============================================================
# 2. 读取目标智能表格的字段
# ============================================================
def get_fields(access_token):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/get_fields?access_token={access_token}"
    resp = requests.post(
        url=url, json={"docid": DST_DOCID, "sheet_id": DST_SHEET_ID}, timeout=30
    ).json()
    if resp.get("errcode") != 0:
        print(f"获取字段失败: {resp}")
        return []
    fields = resp.get("fields", [])
    print(f"目标表格字段 {len(fields)} 个：")
    for f in fields:
        print(f"  {f.get('field_title')}  ->  {f.get('field_type')}  (id={f.get('field_id')})")
    return fields


def build_value(field_type, raw):
    """按字段类型组装 add_records 的 value"""
    raw = (raw or "").strip()
    if field_type == 'FIELD_TYPE_TEXT':
        return [{"type": "text", "text": raw}]
    if field_type == 'FIELD_TYPE_URL':
        return [{"type": "url", "text": raw, "link": raw}]
    if field_type == 'FIELD_TYPE_NUMBER':
        try:
            return float(raw) if raw else None
        except ValueError:
            return None
    if field_type == 'FIELD_TYPE_CHECKBOX':
        return raw.lower() in ("true", "1", "是", "yes") if raw else False
    if field_type == 'FIELD_TYPE_DATE_TIME':
        # 先按可读时间解析；纯数字按时间戳（秒/毫秒）处理
        if raw.isdigit():
            ts = int(raw)
            ts = ts * 1000 if ts < 10 ** 12 else ts
            return str(ts)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return str(int(datetime.strptime(raw, fmt).timestamp() * 1000))
            except ValueError:
                continue
        return None
    if field_type == 'FIELD_TYPE_USER':
        return [{"user_id": raw}] if raw else []
    if field_type == 'FIELD_TYPE_SINGLE_SELECT':
        return [{"text": raw}] if raw else []
    return raw


# ============================================================
# 3. 写入智能表格
# ============================================================
def add_records(access_token, values):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/add_records?access_token={access_token}"
    payload = {
        "docid": DST_DOCID,
        "sheet_id": DST_SHEET_ID,
        "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
        "records": [{"values": values}],
    }
    print(f"请求包体: {json.dumps(payload, ensure_ascii=False, indent=2)}")
    if DRY_RUN:
        print("DRY_RUN=True，未真正写入")
        return None

    resp = requests.post(url=url, json=payload, timeout=30).json()
    print(f"add_records 返回: {resp}")
    if resp.get("errcode") != 0:
        return None
    records = resp.get("records", [])
    if records:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"写入成功，record_id={records[0].get('record_id')}，已存 {CACHE_FILE}")
    return records


# ============================================================
# 主流程
# ============================================================
def main():
    token = get_valid_token()
    if not token:
        print("获取 access_token 失败")
        return

    fields = get_fields(token)
    if not fields:
        return
    type_map = {f.get("field_title"): f.get("field_type") for f in fields}

    blocks = list_blocks(token, SMART_DOC_ID, PAGE_ID)
    lines = filter_record_lines(blocks)
    print(f"解析出记录行 {len(lines)} 条：")
    for i, ln in enumerate(lines, 1):
        print(f"  [{i}] {ln}")

    if len(lines) < TARGET_INDEX:
        print(f"只有 {len(lines)} 条记录，取不到第 {TARGET_INDEX} 条")
        return

    target = lines[TARGET_INDEX - 1]
    parsed = parse_line(target, type_map.keys())
    print(f"\n第 {TARGET_INDEX} 条解析结果: {parsed}")

    values = {}
    for title, raw in parsed.items():
        ftype = type_map.get(title)
        if not ftype or ftype in SKIP_FIELD_TYPES:
            print(f"  跳过字段 {title}（{ftype or '表格中不存在'}）")
            continue
        val = build_value(ftype, raw)
        if val is None:
            print(f"  跳过字段 {title}（值为空或无法转换）")
            continue
        values[title] = val

    if not values:
        print("没有可写入的字段，终止")
        return

    add_records(token, values)


if __name__ == "__main__":
    main()
