# -*- coding: utf-8 -*-
"""
G-20-客户订单总表
从 SQL Server 读取「客户订单」数据，同步写入企业微信智能表格（子表 NROy5V）。

运行方式：常驻进程，运行窗口 9:00 ~ 24:00，每 5 分钟同步一次；启动立即执行一次。
每轮流程：
    1. 按单号前缀取最近 3 天的订单（新增 + 修改都在这个范围）
    2. 按「流水单号」比对智能表已有记录 → 新增 / 更新（全字段覆盖）
    3. 删除超出保留范围（最近 3 个月）以外的旧记录
    4. 顺序：先新增 → 再更新 → 最后删除

数据源：SQL Server 宽表视图 N_BILL_LIST_DAT_XSD_DQ_NEW + 基表 DAT_XSD_DQ（补最后修改时间）
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

# ---- SQL Server 连接（★ TODO 待确认端口与账号）----
DB_SERVER = '43.139.103.210,14613'
DB_DATABASE = 'dq2011new'
DB_USERNAME = 'Testk9s8n54r'
DB_PASSWORD = 'H4p%vE7c#kM2h!uP5'

# ---- 企业微信凭证（此文档 dcXZDwy 属于 main 应用，不能用 G-21 的 yushou 应用，2640005=access denied）----
CORPID = 'ww72152ae3c89a4de5'
SECRET = '_GMF08y3ZGtxVtLj6r3Zhn0p4hJjSWc_DlGmicZl99I'

# ---- 智能表格 ----
DOCID = 'dcXZDwyKcA58RVDccVzGP9JHTUfUFXxNaBUPnjdCNfLBjqziRpI2dIvBP8oUnW9Xnx6PgieOdTvpH-T74anbWEgg'
SHEET_ID = 'NROy5V'   # 订单总表子表
UNIQUE_FIELD = '流水单号'   # 总表智能表没有 ID 列，用流水单号做唯一键

# ---- 同步参数 ----
# 同步模式：增量 —— 按单号前缀取最近 INCREMENT_DAYS 天的订单，全字段覆盖更新（新增 + 修改）

BATCH_SIZE = 100           # 每批写入/更新条数，避免超过 10M 限制
SYNC_INTERVAL_MINUTES = 5

# ---- 保留范围（删除用）----
# 每轮同步在「新增 + 更新」之后，删掉超出保留范围的旧记录：
# 按唯一键（流水单号）前 4 位 = 月份码（yymm）判断，不在最近 KEEP_MONTHS 个月内的行删除
DELETE_OUT_OF_RANGE = True
KEEP_MONTHS = 3            # 只保留最近 3 个月（如 2610/2609/2608）

# ---- SQL 查询 ----
# 数据源：N_BILL_LIST_DAT_XSD_DQ_NEW（预处理宽表视图，与 D:\php-sync 的 Sync.php 同源）
# 视图已把客户名称、门店分类、制单员/开单人/发货人姓名、转寄信息、需打印报表等
# 全部 JOIN/转换好，且列名就是智能表的中文列名 —— 无需再做 JOIN。
VIEW_NAME = 'N_BILL_LIST_DAT_XSD_DQ_NEW'

# 增量模式的更新窗口（天）
INCREMENT_DAYS = 3

# 最后修改时间（sys_moddate）宽表视图里没有，需关联基表 DAT_XSD_DQ 按单号取得。
# 转成 'YYYY-MM-DD HH:MM:SS' 文本写入智能表「最后编辑时间」列。
# 注意：该列只写入展示，【不做】增量筛选条件 —— 增量按单号前缀过滤。
JOIN_CLAUSE = "LEFT JOIN DAT_XSD_DQ h ON h.[billno] = v.[流水单号]"
MOD_COL = "CONVERT(varchar(19), h.[sys_moddate], 120) AS [最后编辑时间]"

def increment_billno_prefix():
    """增量起点单号前缀：N 天前的日期码。单号前 6 位 = yymmdd，如 261013 = 2026-10-13"""
    return (datetime.now() - timedelta(days=INCREMENT_DAYS)).strftime('%y%m%d')

def build_query():
    """生成增量 SQL：按单号前缀取最近 N 天（新增 + 修改的订单都在这个范围里）"""
    # 单号前 6 位 = yymmdd（261013 = 2026-10-13），>= N 天前的日期码即最近 N 天
    prefix = increment_billno_prefix()
    print(f"增量起点单号前缀: {prefix}（最近 {INCREMENT_DAYS} 天）")
    return (f"SELECT v.*, {MOD_COL} FROM {VIEW_NAME} v {JOIN_CLAUSE} "
            f"WHERE v.[流水单号] >= '{prefix}' "
            f"ORDER BY v.[流水单号] DESC")

def keep_month_prefixes():
    """保留范围：最近 KEEP_MONTHS 个月的单号前 4 位（yymm），如 ['2610','2609','2608']"""
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
#   图片/图片1/图片3 —— 智能表是图片类型列，不能写文本
#   产品分类/小供名-去重引用 —— 智能表自动字段，不可写入
#   1成交总额/1单价审核/复选框/多选/跟单员1/关联/跟单员分类/订单类型/订单月份/延误时间
#   —— 宽表里没有对应来源
FIELD_MAPPING = {
    '流水单号': '流水单号',
    '客户':     '客户',
    '门店分类': '门店分类',
    '联系人':   '联系人',
    '手机':     '手机',
    '电话':     '电话',
    '货号':     '货号',
    '颜色':     '颜色',
    '颜备':     '颜备',
    '备外':     '备外',
    '仓备':     '仓备',
    '门备':     '门备',
    '财备':     '财备',
    '出库数量': '出库数量',
    '应收金额': '应收金额',
    '其它金额': '其它金额',
    '收款金额': '收款金额',
    '本单未收': '本单未收',
    '制单日期': '制单日期',
    '新增单时间': '新增单时间',
    '开单时间': '开单时间',
    '发货时间': '发货时间',
    '到店时间': '到店时间',
    '要求提货时间': '要求提货时间',
    '制单员':   '制单员',
    '开单人':   '开单人',
    '发货人':   '发货人',
    '打印人':   '打印人',
    '交代人':   '交代人',
    '跟单人':   '跟单人',
    '主要跟单人': '主要跟单人',
    '跟单员':   '跟单员',
    '跟进微信': '跟进微信',
    '跟进单号': '跟进单号',
    '商城单号': '商城单号',
    '收款帐户': '收款帐户',
    '需打印报表': '需打印报表',
    '发货方式': '发货方式',
    '货运方向': '货运方向',
    '物流单号': '物流单号',
    '地址':     '地址',
    '地址2':    '地址2',
    '前小配':   '前小配',
    '后小配':   '后小配',
    '公斤数':   '公斤数',
    '匹数':     '匹数',
    '转寄':     '转寄',
    '转寄客户': '转寄客户',
    '转寄联系人': '转寄联系人',
    '转寄人手机': '转寄人手机',
    '转寄电话': '转寄电话',
    '转寄地址': '转寄地址',
    '图片2':    '图片2',    # 智能表「图片2」是文本列，可写；图片/图片1/图片3 是图片列，跳过
    '已交代':   '已交代',
    '剪好':     '剪好',
    '到店':     '到店',
    '已开单':   '已开单',
    '发货':     '发货',
    '打印':     '打印',
    '代收':     '代收',
    '转寄':     '转寄',
    '有效':     '有效',
    '寄付':     '寄付',
    '退货':     '退货',
    '导入爱迅': '导入爱迅',
    '必须开单': '必须开单',
    '急单':     '急单',
    '超时':     '超时',
    '审核':     '审核',
    '全部交代': '全部交代',
    '微信关注': '微信关注',
    '最后编辑时间': '最后编辑时间',   # 基表 DAT_XSD_DQ.sys_moddate，增量同步的窗口依据
}

# ---- 智能表列类型适配（按 get_fields 实测结果，2026-10-12 核对）----
# number  = 智能表是数字/货币列，写数字
# date    = 智能表是日期列，写毫秒时间戳字符串（宽表里的 varchar 时间会自动解析）
# bit01   = 宽表是 bit、智能表是文本列：写 '1'/'0'（与 php-sync 老表写法一致）
# 未列出的 = 文本列，直接写文本
FIELD_TYPES = {
    # 数字列
    '应收金额': 'number', '收款金额': 'number', '本单未收': 'number',
    '出库数量': 'number', '公斤数': 'number', '匹数': 'number',
    '超时': 'number',       # bit → 1/0
    '电话': 'number', '转寄电话': 'number',   # 宽表是文本，能转数字就写数字
    # 日期列
    '制单日期': 'date', '新增单时间': 'date', '开单时间': 'date',
    '发货时间': 'date', '到店时间': 'date',
    # bit → '1'/'0'（智能表是文本列）
    '已交代': 'bit01', '剪好': 'bit01', '到店': 'bit01', '已开单': 'bit01',
    '发货': 'bit01', '打印': 'bit01', '代收': 'bit01', '转寄': 'bit01',
    '有效': 'bit01', '寄付': 'bit01', '退货': 'bit01', '导入爱迅': 'bit01',
    '必须开单': 'bit01', '急单': 'bit01', '审核': 'bit01',
    '全部交代': 'bit01', '微信关注': 'bit01',
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

        # 2. 执行查询（增量：按单号前缀取最近 N 天）
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
        return None  # 解析不了的格式跳过，避免整批失败

    # ---- 文本列 ----
    if isinstance(value, bool):
        return [{"type": "text", "text": "1" if value else "0"}]
    if isinstance(value, bytes):
        # rowversion 版本号 → 数字字符串
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
            skipped += 1  # 唯一键（流水单号）为空，无法匹配，跳过
    if skipped:
        print(f"警告：{skipped} 条记录的唯一键「{UNIQUE_FIELD}」为空，已跳过")
    # 4. 删除：智能表里超出保留范围（最近 KEEP_MONTHS 个月）的旧记录
    #    只按唯一键前 4 位月份码判断，且在第 5 步「先新增/更新、后删除」，不会误删本次写入的数据
    keep_prefixes = keep_month_prefixes()
    to_delete = []
    if DELETE_OUT_OF_RANGE:
        for uid, record_id in existing_map.items():
            # uid = 流水单号，前 4 位是月份码；长度不足 4 位的脏数据不处理，避免误删
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
# ★ 定时增量同步（按单号前缀取最近 INCREMENT_DAYS 天）
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
