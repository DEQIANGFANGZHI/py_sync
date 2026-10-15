# -*- coding: utf-8 -*-
"""
G-20-客户订单明细
从 SQL Server 读取「客户订单明细」数据，同步写入企业微信智能表格（子表 0aWiRN）。

运行方式：常驻进程，运行窗口 9:00 ~ 24:00，每 5 分钟同步一次；启动立即执行一次。
每轮流程：
    1. 按 ID（单号+行号）前缀取最近 3 天的明细行（新增 + 修改都在这个范围）
    2. 按「ID」比对智能表已有记录 → 新增 / 更新（全字段覆盖）
    3. 删除超出保留范围（最近 2 个月）以外的旧记录
    4. 顺序：先新增 → 再更新 → 最后删除

数据源：SQL Server 宽表视图 N_BILL_LIST_DAT_XSD_DQ_DTL_NEW
        + 左连总表视图（跟单员）/ 明细基表（剪好·已交代·发货·到店）/ 订单基表（退布）
需要改的配置全部集中在文件顶部「配置区」。
"""

import time
import struct
from datetime import datetime, timedelta
from decimal import Decimal

import pyodbc
import requests
import schedule

# ============================================================
# ★ 配置区：需要改的东西全在这一块
# ============================================================

# ---- 运行时间窗口：9:00 ~ 24:00（小时 >= 9 且 < 24）----
RUN_START_HOUR = 9
RUN_END_HOUR = 24

# ---- SQL Server 连接
DB_SERVER = '43.139.103.210,14613'
DB_DATABASE = 'dq2011new'
DB_USERNAME = 'Testk9s8n54r'
DB_PASSWORD = 'H4p%vE7c#kM2h!uP5'

# ---- 企业微信凭证（此文档 dcXZDwy 属于 main 应用，不能用 G-21 的 yushou 应用，2640005=access denied）----
CORPID = 'ww72152ae3c89a4de5'
SECRET = '_GMF08y3ZGtxVtLj6r3Zhn0p4hJjSWc_DlGmicZl99I'

# ---- 智能表格 ----
DOCID = 'dcXZDwyKcA58RVDccVzGP9JHTUfUFXxNaBUPnjdCNfLBjqziRpI2dIvBP8oUnW9Xnx6PgieOdTvpH-T74anbWEgg'
SHEET_ID = '0aWiRN'   # 订单明细子表
UNIQUE_FIELD = 'ID'   # 智能表有 ID 列，值 = 单号 + 行号（SQL 里拼好的）

# ---- 同步参数 ----
# 同步模式：增量 —— 按 ID 前缀取最近 INCREMENT_DAYS 天的明细行，全字段覆盖更新（新增 + 修改）

BATCH_SIZE = 100           # 每批写入/更新条数，避免超过 10M 限制
SYNC_INTERVAL_MINUTES = 5

# ---- 保留范围（删除用）----
# 每轮同步在「新增 + 更新」之后，删掉超出保留范围的旧记录：
# 按唯一键（ID = 单号+行号）前 4 位 = 月份码（yymm）判断，不在最近 KEEP_MONTHS 个月内的行删除
DELETE_OUT_OF_RANGE = True
KEEP_MONTHS = 2            # 只保留最近 2 个月（如 2610/2609）

# ---- SQL 查询 ----
# 数据源：N_BILL_LIST_DAT_XSD_DQ_DTL_NEW（预处理宽表视图，与 D:\php-sync 的 Syncm.php 同源）
# 视图已把货号/颜色/颜备/供编/小供名/前状态/后状态/方向等全部 JOIN 好并转为中文名，
# ID 列（单号+行号）视图里现成有，列名就是智能表的中文列名 —— 无需再 JOIN 布料/颜色表。
# 增量：按单号+行号（ID）前缀过滤，取最近 INCREMENT_DAYS 天的明细行。
# 注：智能表里新增的 sys_timestamp（版本号）列不写入、也不用于增量 —— 实测该版本号有重复/跳跃，
# 不可靠；改用 ID（单号+行号）前缀。
VIEW_NAME = 'N_BILL_LIST_DAT_XSD_DQ_DTL_NEW'

# 增量模式的起点单号前缀：只取流水单号前 6 位 >= 该值的明细行，再按单号倒序取最近 N 条
# 单号规则：261013 = 2026年10月13日（前4位年月，第5-6位日）
# 增量模式的更新窗口（天）：最近 N 天内变动的明细行，全部取回并全字段覆盖更新
# 与 D:\php-sync 的 syncm/update_ent 逻辑一致（PHP 也是 GETDATE()-3）
INCREMENT_DAYS = 3

# 「跟单员」明细视图里没有，从总表视图按单号关联取得（已验证：16758 行全部取到，单号不重复）
# 「剪好/已交代/发货」取自明细基表 DAT_XSD_DQ_DTL（按 billno + row_id = ID 关联，确保行级唯一）
# 「到店」基表只有 if_daohuo（到货），按此映射
# 「退布」明细基表没有该字段，取总表 DAT_XSD_DQ.tuihuo（整单退货标志）按单号关联
JOIN_CLAUSE = (
    "LEFT JOIN N_BILL_LIST_DAT_XSD_DQ_NEW h ON d.[流水单号] = h.[流水单号] "
    "LEFT JOIN DAT_XSD_DQ_DTL t ON t.[billno] = d.[流水单号] "
    "AND RIGHT(d.[ID], LEN(d.[ID]) - LEN(d.[流水单号])) = CONVERT(varchar(10), t.[row_id]) "
    "LEFT JOIN DAT_XSD_DQ h2 ON h2.[billno] = d.[流水单号]"
)
GEN_COL = (
    "h.[跟单员] AS [跟单员], "
    "t.[if_jianhao] AS [剪好], t.[if_jiaodai] AS [已交代], t.[if_fahuo] AS [发货], "
    "t.[if_daohuo] AS [到店], h2.[tuihuo] AS [退布]"
)

def increment_billno_prefix():
    """增量起点前缀：N 天前的日期码。ID = 单号(前6位 yymmdd) + 行号，如 2610130742"""
    return (datetime.now() - timedelta(days=INCREMENT_DAYS)).strftime('%y%m%d')

def build_query():
    """生成增量 SQL：按 ID 前缀取最近 N 天（新增 + 修改的明细行都在这个范围里）"""
    # ID = 单号(前6位 yymmdd) + 行号，>= N 天前的日期码即最近 N 天
    prefix = increment_billno_prefix()
    print(f"增量起点前缀: {prefix}（最近 {INCREMENT_DAYS} 天）")
    return (f"SELECT d.*, {GEN_COL} FROM {VIEW_NAME} d {JOIN_CLAUSE} "
            f"WHERE d.[ID] >= '{prefix}' "
            f"ORDER BY d.[ID] DESC")

def keep_month_prefixes():
    """保留范围：最近 KEEP_MONTHS 个月的月份码（yymm），如 ['2610','2609']；取 ID 前 4 位比对"""
    now = datetime.now()
    prefixes = []
    for i in range(KEEP_MONTHS):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        prefixes.append(f"{y % 100:02d}{m:02d}")
    return prefixes

# ---- 字段映射：宽表列名 → 智能表格列名（两者一致，恒等映射）----
# 未写入的智能表列及原因：
#   仓备/要求提货时间/发货方式/图片1-去重引用/客户-去重引用/产品分类/最后编辑时间
#   —— 智能表是查找引用/公式自动字段，不可写入
#   客户/门店/引用-物流区域/延误时间/销售大货价对比开单价格 —— 宽表里没有对应来源
#   （退布/发货/已交代/剪好/到店 已改为关联基表取值写入）
FIELD_MAPPING = {
    'ID':        'ID',        # 视图现成列：单号 + 行号
    '流水单号':  '流水单号',
    '跟单员':    '跟单员',     # JOIN 总表视图取得（明细视图本身没有该列）
    '货号':      '货号',
    '颜色':      '颜色',
    '颜备':      '颜备',
    '单位':      '单位',
    '供编':      '供编',
    '小供编':    '小供编',
    '小供名':    '小供名',
    '方向':      '方向',
    '备注':      '备注',
    '前小配':    '前小配',
    '后小配':    '后小配',
    '前状态':    '前状态',
    '后状态':    '后状态',
    '出库数量':  '出库数量',
    '过帐数量':  '过帐数量',
    '单价':      '单价',
    '金额':      '金额',
    '销售大货价': '销售大货价',
    '二批价':    '二批价',
    '日期':      '日期',
    '有效':      '有效',
    '跟单员':    '跟单员',     # JOIN 总表视图取得（明细视图本身没有该列）
    '剪好':      '剪好',       # 基表 if_jianhao
    '已交代':    '已交代',     # 基表 if_jiaodai
    '发货':      '发货',       # 基表 if_fahuo
    '到店':      '到店',       # 基表 if_daohuo（到货）
    '退布':      '退布',       # 总表 tuihuo（整单退货标志）
    # sys_timestamp（版本号）不写入智能表 —— 实测不准，增量改按「日期」窗口 + ID 倒序
}

# ---- 智能表列类型适配（按 get_fields 实测结果，2026-10-12 核对）----
# number = 数字列；date = 日期列（写毫秒时间戳字符串）；bit01 = bit 转 '1'/'0'
# 未列出的 = 文本列（销售大货价/二批价 的 decimal 会自动转字符串）
FIELD_TYPES = {
    '出库数量': 'number',
    '过帐数量': 'number',
    '单价':     'number',
    '金额':     'number',
    '日期':     'date',
    '有效':     'bit01',
    '剪好':     'bit01',   # 基表 bit → 智能表文本列 '1'/'0'
    '已交代':   'bit01',
    '发货':     'bit01',
    '到店':     'bit01',
    '退布':     'bit01',
}


# ============================================================
# 以下为通用逻辑，配置填好后无需改动
# ============================================================


def is_in_runtime_window():
    """判断当前是否在允许的运行时间窗口内"""
    now = datetime.now()
    return RUN_START_HOUR <= now.hour < RUN_END_HOUR


def get_sqlserver_connection():
    try:
        connection_string = (
            f'DRIVER={{SQL Server}};SERVER={DB_SERVER};'
            f'DATABASE={DB_DATABASE};UID={DB_USERNAME};PWD={DB_PASSWORD}'
        )
        return pyodbc.connect(connection_string)
    except pyodbc.Error as e:
        print(f"连接数据库时出错: {e}")


def get_shuju():
    conn = None
    cursor = None
    try:
        # 1. 建立数据库连接
        conn = get_sqlserver_connection()
        if not conn:
            print("连接SQL Server数据库失败")
            return None, []
        print("成功连接到SQL Server数据库")
        cursor = conn.cursor()

        # 2. 执行查询（增量：按 ID 前缀取最近 N 天）
        query = build_query()
        print(f"执行查询（增量，最近 {INCREMENT_DAYS} 天）")
        cursor.execute(query)
        rows = cursor.fetchall()

        # 获取字段名列表
        field_names = [column[0] for column in cursor.description]

        # 3. 获取并打印 session_id（统一位置）
        cursor.execute("SELECT @@SPID AS session_id")
        spid_row = cursor.fetchone()
        session_id = spid_row[0] if spid_row else None
        print(f"当前连接的session_id: {session_id}，共 {len(rows)} 条")

        return rows, field_names
    except pyodbc.Error as e:
        print(f"执行查询时出错: {e}")
        return None, []

    finally:
        # 4. 统一关闭游标和连接
        if cursor:
            try:
                cursor.close()
                print("cursor已关闭")
            except pyodbc.Error as e:
                print(f"关闭游标失败: {e}")
        if conn:
            try:
                conn.close()
                print("conn已关闭")
            except pyodbc.Error as e:
                print(f"关闭连接失败: {e}")


# print(get_shuju())


def convert_value(value, ftype):
    """把宽表值转换成智能表写入值；返回 None 表示该字段这条记录跳过不写"""
    if value is None or value == "":
        return None

    if ftype == 'bit01':
        # bit → '1'/'0'（智能表是文本列，必须用文本列表格式，裸字符串会被企微丢弃）
        return [{"type": "text", "text": "1" if value else "0"}]

    if ftype == 'number':
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        try:
            return float(str(value).strip())
        except (ValueError, TypeError):
            return None  # 非数字内容写数字列会导致整批失败，跳过

    if ftype == 'date':
        if isinstance(value, datetime):
            return str(int(value.timestamp() * 1000))
        s = str(value).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                    '%Y/%m/%d %H:%M:%S', '%Y/%m/%d'):
            try:
                return str(int(datetime.strptime(s, fmt).timestamp() * 1000))
            except ValueError:
                pass
        return None

    # ---- 文本列 ----
    if isinstance(value, bool):
        return [{"type": "text", "text": "1" if value else "0"}]
    if isinstance(value, bytes):
        # rowversion（LastChanged）→ 数字字符串
        if len(value) == 8:
            return [{"type": "text", "text": str(struct.unpack('>Q', value)[0])}]
        return None
    if isinstance(value, datetime):
        return [{"type": "text", "text": value.strftime('%Y-%m-%d %H:%M:%S')}]
    return [{"type": "text", "text": str(value)}]


def gouzhao_shuju():
    """把宽表行构造成智能表格写入格式"""
    shuju, field_names = get_shuju()
    record_data = []
    if not shuju:
        return record_data

    for row in shuju:
        data = {"values": {}}
        for i, field_name in enumerate(field_names):
            # 通过映射获取智能表格中的列名，无映射则跳过该字段
            cn_name = FIELD_MAPPING.get(field_name)
            if cn_name is None:
                continue
            converted = convert_value(row[i], FIELD_TYPES.get(cn_name, 'text'))
            if converted is not None:
                data["values"][cn_name] = converted
        record_data.append(data)
    return record_data


# print(gouzhao_shuju())


def get_token():
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORPID}&corpsecret={SECRET}"
    response = requests.get(url).json()
    # 返回 access_token
    if response["errcode"] == 0:
        return {
            "token": response["access_token"],
            "expires_in": 7000
        }
    else:
        print(f"获取token失败: {response['errcode']}")


token_info = {}  # token未过期复用，过期则重新取值


def get_valid_token():
    global token_info
    if not token_info or time.time() >= token_info.get('expiry_time', 0):
        new_token = get_token()
        if new_token is None:
            return ""
        token_info = {
            "token": new_token["token"],
            "expiry_time": time.time() + new_token["expires_in"]
        }
    return token_info["token"]


def query_records(access_token, field_titles=None):
    """分页拉取智能表格全部记录，返回记录列表（含 record_id 和指定字段）"""
    if field_titles is None:
        field_titles = [UNIQUE_FIELD]
    all_records = []
    offset = 0
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/get_records?access_token={access_token}"
    while True:
        data = {
            "docid": DOCID,
            "sheet_id": SHEET_ID,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "field_titles": field_titles,
            "offset": offset,
        }
        response = requests.post(url=url, json=data).json()
        if response.get("errcode", 0) != 0:
            print(f"查询记录失败: {response}")
            break
        records = response.get("records", [])
        all_records.extend(records)

        if not response.get("has_more"):
            break
        offset = response.get("next", 0)

    print(f"查询现有记录，共 {len(all_records)} 条")
    return all_records


def add_records(records, access_token):
    """新增记录，分批发送避免超 10M 限制"""
    if not records:
        print("无需新增记录")
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/add_records?access_token={access_token}"
    total = len(records)
    for i in range(0, total, BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        data = {
            "docid": DOCID,
            "sheet_id": SHEET_ID,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "records": batch
        }
        response = requests.post(url=url, json=data).json()
        print(f"新增 {i + 1}-{min(i + BATCH_SIZE, total)}/{total}: {response}")
    print(f"新增完成，共 {total} 条")


def update_records(records, access_token):
    """更新记录，分批发送避免超 10M 限制"""
    if not records:
        print("无需更新记录")
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/update_records?access_token={access_token}"
    total = len(records)
    for i in range(0, total, BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        data = {
            "docid": DOCID,
            "sheet_id": SHEET_ID,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "records": batch
        }
        response = requests.post(url=url, json=data).json()
        print(f"更新 {i + 1}-{min(i + BATCH_SIZE, total)}/{total}: {response}")
    print(f"更新完成，共 {total} 条")


def extract_field_value(record, field_name):
    """从记录中提取字段值，兼容文本格式 [{"type":"text","text":"..."}] 和直接赋值格式"""
    cell = record.get("values", {}).get(field_name)
    if cell is None:
        return None
    if isinstance(cell, list):
        # 文本格式：[{"type": "text", "text": "..."}]
        return cell[0].get("text", "") if cell else ""
    # 直接赋值格式：数字或时间戳字符串
    return cell


def delete_records(record_ids, access_token):
    """删除记录，分批发送避免超 10M 限制"""
    if not record_ids:
        print("无需删除记录")
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/delete_records?access_token={access_token}"
    total = len(record_ids)
    for i in range(0, total, BATCH_SIZE):
        batch = record_ids[i:i + BATCH_SIZE]
        data = {
            "docid": DOCID,
            "sheet_id": SHEET_ID,
            "record_ids": batch
        }
        response = requests.post(url=url, json=data).json()
        if response.get("errcode", 0) != 0:
            print(f"删除失败: {response}")
            continue
        print(f"删除 {i + 1}-{min(i + BATCH_SIZE, total)}/{total}: {response}")
    print(f"删除完成，共 {total} 条")


def sync_records(access_token):
    """核心同步逻辑：以数据库为准，智能表格镜像 SQL 结果"""
    # 1. 获取 SQL Server 数据
    record_data = gouzhao_shuju()
    if not record_data:
        print("SQL Server 无数据，跳过同步")
        return

    # 2. 分页拉取智能表格全部记录，构建 唯一键 → record_id 映射
    existing_records = query_records(access_token)
    existing_map = {}
    for rec in existing_records:
        uid = extract_field_value(rec, UNIQUE_FIELD)
        if uid:
            existing_map[str(uid)] = rec["record_id"]

    # 3. 收集 SQL 数据的唯一键集合，区分新增和更新
    sql_ids = set()
    to_add = []
    to_update = []
    skipped = 0
    for rec in record_data:
        uid = extract_field_value(rec, UNIQUE_FIELD)
        if uid:
            sql_ids.add(str(uid))
            if str(uid) in existing_map:
                to_update.append({
                    "record_id": existing_map[str(uid)],
                    "values": rec["values"]
                })
            else:
                to_add.append(rec)
        else:
            skipped += 1  # 唯一键（ID）为空，无法匹配，跳过
    if skipped:
        print(f"警告：{skipped} 条记录的唯一键「{UNIQUE_FIELD}」为空，已跳过")
    # 4. 删除：智能表里超出保留范围（最近 KEEP_MONTHS 个月）的旧记录
    #    只按唯一键前 4 位月份码判断，且在第 5 步「先新增/更新、后删除」，不会误删本次写入的数据
    keep_prefixes = keep_month_prefixes()
    to_delete = []
    if DELETE_OUT_OF_RANGE:
        for uid, record_id in existing_map.items():
            # uid = ID（单号+行号），前 4 位是月份码；长度不足 4 位的脏数据不处理，避免误删
            if len(uid) >= 4 and uid[:4] not in keep_prefixes:
                to_delete.append(record_id)
        if to_delete:
            print(f"保留月份: {keep_prefixes}，超出范围的旧记录 {len(to_delete)} 条待删除")

    # 5. 批量执行
    print(f"比对结果：新增 {len(to_add)} 条，更新 {len(to_update)} 条，删除 {len(to_delete)} 条")
    add_records(to_add, access_token)
    update_records(to_update, access_token)
    delete_records(to_delete, access_token)


# ============================================================
# 主循环：仅在 9:00 ~ 24:00 运行，每 5 分钟同步一次
# ============================================================


def scheduled_task():
    """定时任务：窗口内执行同步，窗口外只打日志"""
    if is_in_runtime_window():
        try:
            sync_records(get_valid_token())
        except Exception as e:
            print(f"同步出错: {e}")
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 不在运行窗口内，等待中...")


print(f"运行时间窗口: {RUN_START_HOUR}:00 ~ {RUN_END_HOUR}:00")

# ============================================================
# ★ 定时增量同步（按 ID 前缀取最近 INCREMENT_DAYS 天）
#   1. 启动立即执行一次
#   2. 之后每 SYNC_INTERVAL_MINUTES 分钟执行一次（仅运行窗口内）
# ============================================================
print(f"定时同步启动，每 {SYNC_INTERVAL_MINUTES} 分钟一次，启动先跑一次...")
try:
    sync_records(get_valid_token())
except Exception as e:
    print(f"首次同步出错: {e}")

schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(scheduled_task)

while True:
    schedule.run_pending()
    time.sleep(1)
