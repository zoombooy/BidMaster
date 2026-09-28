"""语义判定对照实验：当前能力 vs 关键词原型 vs Qwen 语义判定。
口径全部来自真实国网招标文件原文；业绩库为模拟企业业绩（人工标注预期）。"""
import json, re, sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from dotenv import dotenv_values
import httpx

env = dotenv_values('.env')

# ---------- 真实评分口径（国网第2-6章/公告原文摘录） ----------
CRITERIA = [
    {"id": "K1", "lot": "换流站土建施工", "text":
     "开标前5年内：换流站土建A包：企业具有±800kV换流站土建A包业绩的，如有业绩得3分，如无得0分"},
    {"id": "K2", "lot": "资格条件", "text":
     "近5年内须完成至少2项单项合同金额5000万元及以上的同类房屋建筑工程业绩"},
    {"id": "K3", "lot": "结算审核", "text":
     "开标前5年内企业具有1000kV变电站工程监理业绩的，得3分"},
]

# ---------- 模拟企业业绩库（含人工标注的预期判定） ----------
PERFORMANCES = [
    {"id": "P1", "desc": "±800kV某某换流站土建施工工程，合同金额12000万元，2024年6月竣工，独立承包",
     "expect": {"K1": "match", "K2": "uncertain", "K3": "no_match"}},
    {"id": "P2", "desc": "1000kV某某变电站电气安装工程，合同金额8000万元，2023年竣工",
     "expect": {"K1": "no_match", "K2": "uncertain", "K3": "no_match"}},
    {"id": "P3", "desc": "500kV某某变电站新建工程，合同金额6000万元，2022年12月竣工验收",
     "expect": {"K1": "no_match", "K2": "no_match", "K3": "no_match"}},
    {"id": "P4", "desc": "参与多项大型电网工程建设，具有丰富的电网工程实施经验",
     "expect": {"K1": "uncertain", "K2": "uncertain", "K3": "uncertain"}},
    {"id": "P5", "desc": "某某医院门诊综合楼房屋建筑工程，合同金额6000万元，2024年3月竣工",
     "expect": {"K1": "no_match", "K2": "no_match", "K3": "no_match"}},
    {"id": "P6", "desc": "±800kV某某换流站电气安装工程（非土建），合同金额9000万元，2025年竣工",
     "expect": {"K1": "uncertain", "K2": "uncertain", "K3": "no_match"}},
]

# ---------- 方法A：当前规则抽取能力（_perf_parse 口径抽取现状） ----------
from bidmaster.scoring.requirements import _perf_parse
print('=' * 62)
print('A. 当前口径抽取能力（规则层现状）')
print('=' * 62)
for k in CRITERIA:
    parsed = _perf_parse(k["text"]) or {}
    print(f'  {k["id"]} {k["lot"]}: 抽到 {json.dumps(parsed, ensure_ascii=False)}')
print('  → 缺口：scope（电压等级/工程类型/标包细分）完全没有结构化，无法支撑匹配')
print()

# ---------- 方法B：关键词匹配原型（当前思路的极限） ----------
VOLT = re.compile(r'[±±+]?\s*\d{3}\s*kV|±\d{3}kV')
def keyword_match(crit_text, perf_desc):
    t, p = crit_text, perf_desc
    # 电压等级
    cv = set(v.upper().replace(' ', '') for v in VOLT.findall(t))
    pv = set(v.upper().replace(' ', '') for v in VOLT.findall(p))
    if cv and pv and not (cv & pv):
        # ±800kV vs 800kV 归一
        def norm(s): return {x.replace('±', '').replace('+', '') for x in s}
        if not (norm(cv) & norm(pv)):
            return 'no_match', f'电压不符 {sorted(cv)}/{sorted(pv)}'
    # 类型关键词
    for kw, anti in (('换流站', '电气'), ('变电站', ''), ('房屋建筑', '')):
        if kw in t:
            if kw in p:
                if anti and anti in p:
                    return 'no_match', f'{anti}非{kw}'
                return 'match', f'{kw}命中'
            return 'no_match', f'缺{kw}'
    if '电网工程' in p:
        return 'uncertain', '表述模糊'
    return 'uncertain', '关键词未命中'

# ---------- 方法C：Qwen 语义判定 ----------
def qwen_match(crit_text, perf_desc):
    prompt = (f'判定以下企业业绩是否满足招标评分口径，只输出JSON：'
              f'{{"verdict":"match|no_match|uncertain","reason":"一句话"}}\n'
              f'评分口径：{crit_text}\n企业业绩：{perf_desc}')
    r = httpx.post('https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
        headers={'Authorization': 'Bearer ' + env.get('BIDMASTER_LLM_API_KEY', '')},
        json={'model': env.get('BIDMASTER_LLM_MODEL', 'qwen-plus'),
              'messages': [{'role': 'user', 'content': prompt}],
              'temperature': 0, 'max_tokens': 200, 'response_format': {'type': 'json_object'}},
        timeout=60)
    d = json.loads(r.json()['choices'][0]['message']['content'])
    return d.get('verdict', 'uncertain'), d.get('reason', '')[:50]

print('=' * 62)
print('B/C. 三种方法判定矩阵（√=正确 ✗=错误 ?=无法判定）')
print('=' * 62)
hdr = '业绩  口径  人工预期      关键词原型        Qwen语义'
print(hdr)
stats = {'keyword': [0, 0], 'qwen': [0, 0]}  # [对, 总]
rows = []
for p in PERFORMANCES:
    for k in CRITERIA:
        ideal = p['expect'][k['id']]
        kv, kr = keyword_match(k['text'], p['desc'])
        k_ok = (kv == ideal)
        stats['keyword'][0] += k_ok; stats['keyword'][1] += 1
        try:
            qv, qr = qwen_match(k['text'], p['desc'])
        except Exception as e:
            qv, qr = 'ERR', str(e)[:30]
        q_ok = (qv == ideal)
        if qv != 'ERR':
            stats['qwen'][0] += q_ok; stats['qwen'][1] += 1
        mark = lambda v, ideal: '√' if v == ideal else ('?' if v == 'uncertain' else '✗')
        print(f"  {p['id']}   {k['id']}   {ideal:<10}  {kv:<10}{mark(kv, ideal):^2} {kr[:14]:<14}  {qv:<10}{mark(qv, ideal)}")
        rows.append({'perf': p['id'], 'crit': k['id'], 'ideal': ideal,
                     'keyword': kv, 'qwen': qv, 'qwen_reason': qr})

print()
print('=' * 62)
print('统计（对照人工预期）')
print(f"  关键词原型: {stats['keyword'][0]}/{stats['keyword'][1]} 正确"
      f"（{stats['keyword'][0]/stats['keyword'][1]*100:.0f}%）")
if stats['qwen'][1]:
    print(f"  Qwen 语义 : {stats['qwen'][0]}/{stats['qwen'][1]} 正确"
          f"（{stats['qwen'][0]/stats['qwen'][1]*100:.0f}%）")
print()
print('分歧明细（优化方向决策依据）:')
for r in rows:
    if r['keyword'] != r['ideal'] or r['qwen'] != r['ideal']:
        print(f"  {r['perf']}/{r['crit']}: 预期={r['ideal']} 关键词={r['keyword']} Qwen={r['qwen']} | {r['qwen_reason'][:40]}")

# ---------- 方法D：保守判定引擎（scope结构化 + 字段硬判 + 未知转uncertain） ----------
from bidmaster.scoring.scope import extract_scope
from bidmaster.scoring.matcher import match_performance
SCOPES = {
    'K1': (extract_scope(CRITERIA[0]['text']), None, 5),
    'K2': ({'work_types': ['房屋建筑']}, 50_000_000, 5),
    'K3': (extract_scope(CRITERIA[2]['text']), None, 5),
}

def engine_match(kid, perf_desc):
    scope, amin, yrs = SCOPES[kid]
    r = match_performance(perf_desc, scope, amount_min=amin, years=yrs)
    detail = ';'.join(x['detail'][:18] for x in r['report'] if x['state'] != 'unknown')
    return r['verdict'], detail or '要素不足'

print()
print('=' * 62)
print('D. 保守判定引擎（scope结构化+字段硬判+未知转人工）')
print('=' * 62)
d_ok = d_total = d_unc = 0
for p in PERFORMANCES:
    for k in CRITERIA:
        ideal = p['expect'][k['id']]
        dv, dr = engine_match(k['id'], p['desc'])
        ok = (dv == ideal)
        d_ok += ok
        d_total += 1
        if dv == 'uncertain':
            d_unc += 1
        mark = 'V' if ok else ('?' if dv == 'uncertain' else 'X')
        print(f"  {p['id']}  {k['id']}  人工={ideal:<10} 引擎={dv:<10}{mark} {dr[:22]}")
print(f"  保守引擎合计: {d_ok}/{d_total} 正确（{d_ok/d_total*100:.0f}%），uncertain 转人工 {d_unc} 条")

json.dump(rows, open('experiments/semantic_match_result.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('\n明细已存: experiments/semantic_match_result.json')
