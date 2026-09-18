"""用可追溯的本地规则整理单页专家简介，不生成原文没有的资历。"""

import re

from .ooxml import compact


# 长词优先，避免把副主任医师截成主任医师，把博士后截成博士。
CLINICAL = re.compile(r'副主任中医师|主任中医师|副主任医师|主任医师|主治医师|住院医师|副主任护师|主任护师|主管护师')
ACADEMIC = re.compile(
    r'(?:临床医学|临床|医学|肿瘤学)?(?:博士后(?:研究员)?|(?:博士|硕士)(?:研究生)?(?:生)?(?:导师|导)?|研究生导师|研究生导|研究生|硕导|博导)'
    r'|(?:临床)?(?:副)?教授|(?:副)?研究员|访问学者')
SOCIETY = re.compile(r'协会|学会|分会|委员会|专委会|专委|学组|理事|编委|编辑委员会|协作组|促进会|联盟')
SOCIAL_ROLE = re.compile(r'委员|常委|主委|理事|会长|组长|秘书|编委|常务|顾问')
DEPARTMENT = re.compile(r'科|病区|病房|院区|中心|EICU|ICU|院长|院士|MDT', re.I)
HEADINGS = {'学术兼职', '学术任职', '社会兼职', '专业特长', '课题', '科研项目', '研究方向', '获奖', '荣誉'}


def clean(value):
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    # 只去掉列表序号，保留履历中的年份与数量。
    value = re.sub(r'^\s*\d+[.、)]\s*', '', value)
    return re.sub(r'^[\s\-•·◆▪]+', '', value).strip(' \t，,、；;。:：')


def unique(values):
    result = []
    for value in values:
        value = clean(value)
        if value and compact(value) not in {compact(x) for x in result}:
            result.append(value)
    return result


def social_score(value):
    """同类任职先比较机构范围，再比较职务，分数相同时保留原文顺序。"""
    scope = 300 if re.search(r'^(中国|中华|全国|CSCO|国际|亚太)', value, re.I) else 200 if '省' in value else 100
    rank = 90 if re.search(r'(?<!副)(?:主任委员|主委|理事长|会长)', value) else 75 if re.search(r'副主任委员|副主委|副理事长|副会长', value) else 60 if re.search(r'常委|常务|副组长|组长', value) else 40 if '理事' in value else 20
    return scope + rank


def split_other_line(value):
    """从长段原文中提取完整短句，著作信息按书名保留可核对的角色。"""
    books = re.findall(r'《[^》]+》', value)
    if books and re.search(r'著作|编写|主编|出版', value):
        result = []
        for index, book in enumerate(books):
            if index == 0 and '副主编' in value:
                result.append(book + '副主编')
            elif index == 0 and re.search(r'(?<!副)主编', value):
                result.append(book + '主编')
            else:
                result.append('参与' + book + '著作编写')
        return result
    parts = [clean(part) for part in re.split(r'[。；;]+', value) if clean(part)]
    result = []
    for part in parts:
        # 常见长句只删除冗余连接词，保留专业方向、疾病名称、项目层级与数量。
        part = part.replace('主要从事', '从事').replace('的临床与科研工作', '临床科研')
        part = part.replace('等消化内科常见肿瘤的诊治', '诊治')
        part = part.replace('胸部肿瘤的放射治疗相关的', '胸部肿瘤放疗')
        part = part.replace('急危重症尤其是心脏重症的诊治，以及', '心脏重症诊治及')
        part = part.replace('的实施与质控', '')
        part = part.replace('主持山东省科技卫生项目青年基金', '主持省科技卫生青年基金')
        part = part.replace('，中国博士后科研基金', '、博士后科研基金')
        result.append(part)
    return result


def summarize_bio(expert):
    """整理固定八行简介，先放临床信息，再用学校履历和其他原文补足。"""
    source = [clean(x) for x in expert.get('bio', []) if clean(x)]
    if source == ['简介待补充']:
        lines = ['简介待补充', '医院及科室信息待补充', '临床职务职称待补充', '学历信息待补充',
                 '导师信息待补充', '社会任职待补充', '专业方向待补充', '科研成果待补充']
        return {'lines': lines, 'clinical': '', 'academic': '', 'social': [], 'other': [],
                'placeholders': lines[1:], 'source': source, 'missing': ['简介']}
    # 修复文本框里人为拆开的委员会名称，避免输出半条任职。
    joined = []
    for line in source:
        if joined and joined[-1].endswith('委') and line.startswith('会'):
            joined[-1] += line
        else:
            joined.append(line)
    hospital = expert.get('hospital') or expert.get('display_hospital', '')
    # 协会名称中的研究型医院不能当作工作医院。
    if SOCIETY.search(hospital) or not any(hospital and x.startswith(hospital) and not SOCIETY.search(x) for x in joined):
        hospital = expert.get('display_hospital', '')
    clinical, titles, academic, social, other = [], [], [], [], []
    for line in joined:
        if clean(line).rstrip(':：') in HEADINGS:
            continue
        if SOCIETY.search(line) or re.search(r'委员|常委|主委', line):
            if SOCIAL_ROLE.search(line) and not re.search(r'^师从|发表|主持.*基金|擅长', line):
                social.append(line)
            continue
        if re.search(r'^(主要|从事|擅长|发表|主持|承担|专业特长|学术兼职|学术任职|社会兼职|科研|师从|齐鲁人才|济南市劳动)|著作|编写|出版', line):
            if not re.search(r'^(学术兼职|学术任职|社会兼职|专业特长)[:：]?$', line):
                # 长段落按原有句号拆分，保证每一行仍是完整原文。
                other.extend(split_other_line(line))
            continue
        if re.search(r'大学|学院', line) and re.search(r'研究所|实验室|访问学者', line):
            academic.append(line)
            continue
        # 学术头衔可能紧贴临床职称，例如主任医师博士生导师。
        titles.extend(CLINICAL.findall(line))
        rest = clean(CLINICAL.sub('', line).replace('中共党员', ''))
        if not rest:
            continue
        pieces = [clean(p) for p in re.split(r'[，,、；;]+', rest) if clean(p)]
        for piece in pieces:
            match = ACADEMIC.search(piece)
            if match:
                tail = clean(piece[match.end():])
                if match.start() == 0 and tail and DEPARTMENT.search(tail) and not ACADEMIC.search(tail):
                    academic.append(match.group())
                    clinical.append(tail)
                    continue
                prefix = clean(piece[:match.start()])
                # 同一行兼有医院科室和导师头衔时，在头衔起点分开。
                if prefix and ('医院' in prefix or DEPARTMENT.search(prefix)) and not re.search(r'大学|学院', prefix.replace(hospital, '')):
                    clinical.append(prefix)
                    academic.append(piece[match.start():])
                else:
                    academic.append(piece)
            elif hospital and piece == hospital:
                continue
            elif '医院' in piece or DEPARTMENT.search(piece):
                # 医院MDT专长与职务保留在原文中，首行优先主要科室。
                if not re.search(r'MDT|协作|专家', piece):
                    clinical.append(piece)
            else:
                other.append(piece)
    clinical = unique(clinical)
    primary = next((x for x in clinical if hospital and x.startswith(hospital) and x != hospital), '')
    departments = [x for x in clinical if x != primary and '医院' not in x]
    first = primary or hospital
    if departments:
        department = departments[0]
        if department not in first:
            first += department
    for title in unique(titles)[:1]:
        if title not in first:
            first += ('，' if first else '') + title
    # 学历与导师优先于其他学校任职，保留限定词及学校名称，不推断导师层级。
    academic = unique(academic)
    academic.sort(key=lambda x: (0 if re.search(r'导师|硕导|博导|生导', x) else 1 if re.search(r'博士|硕士', x) else 2))
    second = '，'.join(academic[:2])
    social = sorted(unique(social), key=lambda x: -social_score(x))
    other = unique(other)
    missing = []
    if not first:
        missing.append('医院及临床职务')
    if not second:
        missing.append('学历或学校相关履历')
    if not any(DEPARTMENT.search(x.replace(hospital, '')) for x in clinical):
        missing.append('科室')
    if not titles and not any(re.search(r'主任|院长|医师', x) for x in clinical):
        missing.append('临床职务职称')
    # 没有学校履历时直接承接社会任职，不保留空行。其余原文用于补足八行。
    lines = [first] + ([second] if second else [])
    for value in social + other:
        if value and compact(value) not in {compact(x) for x in lines}:
            lines.append(value)
        if len(lines) == 8:
            break
    # 原资料不足八条时用明确的缺项说明补齐，避免编造或重复履历。
    text = '\n'.join(source)
    placeholders = []
    categories = [
        ('学历或学校履历资料未提供', not second),
        ('临床职务职称资料未提供', not titles and not re.search(r'主任|院长|医师', first)),
        ('科室信息资料未提供', not any(DEPARTMENT.search(x.replace(hospital, '')) for x in clinical)),
        ('其他重要任职资料未提供', len(social) < 7),
        ('专业方向资料未提供', not re.search(r'主要|从事|擅长|专业特长', text)),
        ('科研成果资料未提供', not re.search(r'发表|主持|基金|课题|科研|SCI|著作|编写|出版', text, re.I)),
        ('获奖或荣誉资料未提供', not re.search(r'获奖|人才|荣誉|称号', text)),
    ]
    for value, absent in categories:
        if absent and len(lines) < 8:
            lines.append(value)
            placeholders.append(value)
    while len(lines) < 8:
        value = f'其他履历资料待补充{len(lines) + 1}'
        lines.append(value)
        placeholders.append(value)
    selected_social = [x for x in lines if x in social]
    return {'lines': lines[:8], 'clinical': first, 'academic': second, 'social': social, 'other': other,
            'selected_social': selected_social, 'placeholders': placeholders,
            'source': list(expert.get('bio', [])), 'missing': missing}
