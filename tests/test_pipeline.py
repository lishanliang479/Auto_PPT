"""真实样本与边界条件测试，验证匹配、保真复制和失败处理。"""

import copy
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autoppt.analyze import analyze, analyze_template, discover, names_in, parse_agenda, read_expert
from autoppt.bio import summarize_bio
from autoppt.generate import generate, page_plan, photo_frame_shape, validate_model
from autoppt.ooxml import NS, compact, encoded, read_deck, read_package, unpack_experts, xml
from autoppt.server import Handler, TOKEN, ThreadingHTTPServer, apply_edits

ROOT = Path(__file__).resolve().parent.parent


class RuleTests(unittest.TestCase):
    def test_missing_person_after_hospital(self):
        self.assertEqual(names_in('李 磊 教授 单县中心医院 朱 帅 教授 山东大学齐鲁医院', ['李磊']), ['李磊', '朱帅'])
        self.assertEqual(names_in('杨秀婷教授山东大学齐鲁医院崔景利教授山东省肿瘤医院'), ['杨秀婷', '崔景利'])

    def test_adjacent_names_without_separator(self):
        self.assertEqual(names_in('杜忠海教授刘新会教授杨金霞教授赵倩教授祝守慧教授'), ['杜忠海', '刘新会', '杨金霞', '赵倩', '祝守慧'])

    def test_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'bad.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('../outside.docx', b'bad')
            with self.assertRaisesRegex(ValueError, '不安全'):
                unpack_experts(source, Path(temp) / 'safe')
            self.assertFalse((Path(temp) / 'outside.docx').exists())


@unittest.skipUnless((ROOT / 'input1').exists(), '真实资料未放入项目')
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temp.name)
        cls.models = {}
        cls.hashes = {}
        for name in ('input', 'input1'):
            template, agenda, experts = discover(ROOT / name)
            cls.models[name] = analyze(template, agenda, experts, cls.work / name)
            cls.hashes[template] = hashlib.sha256(template.read_bytes()).hexdigest()
        cls.output = cls.work / 'meeting.pptx'
        cls.report = generate(cls.models['input1'], cls.output)
        cls.deck = read_deck(cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_experts_and_sessions(self):
        model = self.models['input1']
        self.assertEqual(len(model['experts']), 16)
        self.assertEqual(model['agenda']['title'], '胸部肿瘤诊疗进展交流会')
        events = model['agenda']['events']
        self.assertEqual([e['kind'] for e in events], ['opening', 'talk', 'discussion', 'talk', 'discussion', 'summary'])
        self.assertEqual(events[1]['hosts'], ['宁方玲'])
        self.assertEqual(events[3]['hosts'], ['时圣彬'])
        self.assertEqual(model['agenda']['people']['宁方玲']['display_title'], '教授')
        self.assertEqual(events[4]['people'], ['杜忠海', '刘新会', '杨金霞', '赵倩', '祝守慧'])

    def test_generation_options_use_expected_defaults(self):
        # 主席简介与待核对模式默认开启，讨论话题页默认关闭。
        options = self.models['input1']['options']
        self.assertTrue(options['repeat_chairs'])
        self.assertFalse(options['include_topics'])
        self.assertTrue(options['draft'])
        self.assertEqual(options['bio_font_size'], 16)

    def test_table_agenda_and_missing_expert(self):
        model = self.models['input']
        self.assertEqual(len(model['agenda']['events']), 6)
        self.assertEqual(model['agenda']['events'][4]['people'], ['李磊', '朱帅'])
        self.assertEqual(model['agenda']['people']['朱帅']['hospital'], '山东大学齐鲁医院德州医院')
        # 关闭待核对模式后，缺少简介仍然需要阻止正式生成。
        model = copy.deepcopy(model)
        model['options']['draft'] = False
        with self.assertRaisesRegex(ValueError, '缺少专家简介：朱帅'):
            validate_model(model)

    def test_paragraph_breaks(self):
        expert = next(e for e in self.models['input1']['experts'] if e['name'] == '尹贻波')
        self.assertTrue(any(line == '山东省医师协会肿瘤精准医疗医师分会委员' for line in expert['bio']))

    def test_table_job_pair(self):
        expert = next(e for e in self.models['input1']['experts'] if e['name'] == '毕经旺')
        self.assertIn('中国医药教育协会罕见病学专业委员会副主任委员', [compact(x) for x in expert['bio']])

    def test_filename_name_fallback(self):
        self.assertTrue(any(i['code'] == 'name_from_filename' and i['expert'] == '王慧君' for i in self.models['input1']['issues']))

    def test_zip_duplicate_inputs(self):
        template, agenda, experts = discover(ROOT / 'input1')
        model = analyze(template, agenda, experts + [ROOT / 'input1/8.13专家简介.zip'], self.work / 'zip')
        self.assertEqual(len(model['experts']), 16)

    def test_output_preserves_dimensions_and_original_parts(self):
        model = self.models['input1']
        self.assertEqual((self.deck['width'], self.deck['height']), (model['template']['width'], model['template']['height']))
        original = read_package(model['template']['path'])
        produced = read_package(self.output)
        for name, data in original.items():
            if name.startswith(('ppt/theme/', 'ppt/slideMasters/', 'ppt/slideLayouts/')) and name in produced:
                self.assertEqual(data, produced[name], name)

    def test_old_people_and_dates_removed(self):
        with zipfile.ZipFile(self.output) as archive:
            payload = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml'))
        for old in ('韩淑梅', '杨正强', '2026年07月21日', '李晓倩'):
            self.assertFalse(old in payload, f'输出仍包含旧内容：{old}')
        self.assertFalse(any('notesSlides/' in name for name in read_package(self.output)))

    def test_selected_bio_is_complete_and_single_page(self):
        model = self.models['input1']
        self.assertEqual(len(self.deck['slides']), len(page_plan(model)))
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            if not spec.get('expert'):
                continue
            self.assertEqual(spec['continuation'], 1)
            self.assertEqual(len(spec['selected_bio']), 8)
            for line in spec['selected_bio']:
                self.assertIn(compact(line), compact(slide['text']))
            source = model['template']['slides'][spec['template_slide'] - 1]
            body = next(s for s in slide['shapes'] if s['id'] == source['fields']['bio'])
            self.assertEqual(body['lines'][0], spec['selected_bio'][0])
            # 专家简介正文固定使用16磅字号。
            self.assertEqual(body['font'], 16)

    def test_summary_keeps_source_in_report(self):
        for expert in self.models['input1']['experts']:
            summary = self.report['bio_summaries'][expert['name']][0]
            self.assertGreaterEqual(len(summary['source']), len(summary['selected_social']))
            self.assertEqual(summary['source'], expert['bio'])
            self.assertEqual(len(summary['lines']), 8)

    def test_mixed_clinical_and_school_credentials(self):
        expert = {'name': '测试', 'hospital': '甲医院', 'bio': [
            '甲医院肿瘤科主任，主任医师博士生导师', '医学博士',
            '某市医学会委员', '中国医学会副主任委员', '某省医学会常委', '某市协会委员']}
        summary = summarize_bio(expert)
        self.assertEqual(summary['lines'][0], '甲医院肿瘤科主任，主任医师')
        self.assertIn('博士生导师', summary['lines'][1])
        self.assertNotIn('医师', summary['lines'][1])
        self.assertEqual(summary['lines'][2], '中国医学会副主任委员')
        self.assertEqual(len(summary['lines']), 8)

    def test_missing_school_is_blank_and_not_invented(self):
        summary = summarize_bio({'name': '测试', 'hospital': '甲医院', 'bio': ['甲医院消化科主治医师', '中国医学会委员']})
        self.assertEqual(summary['lines'][1], '中国医学会委员')
        self.assertIn('学历或学校相关履历', summary['missing'])

    def test_every_summary_has_eight_nonempty_lines(self):
        for model in self.models.values():
            for expert in model['experts']:
                summary = summarize_bio(expert)
                self.assertEqual(len(summary['lines']), 8, expert['name'])
                self.assertTrue(all(line.strip() for line in summary['lines']), expert['name'])
                self.assertFalse(any(line in {'课题', '专业特长', '学术任职'} for line in summary['lines']), expert['name'])

    def test_degree_before_department_and_tcm_title(self):
        summary = summarize_bio({'name': '测试', 'hospital': '甲医院', 'bio': ['甲医院', '博士放疗科主任中医师']})
        self.assertEqual(summary['clinical'], '甲医院放疗科，主任中医师')
        self.assertEqual(summary['academic'], '博士')

    def test_society_hospital_and_research_text_not_clinical(self):
        summary = summarize_bio({'name': '测试', 'hospital': '某省研究型医院', 'display_hospital': '甲医院',
                                 'bio': ['主任医师', '某省研究型医院协会胃肠分会委员', '从事消化科研究工作', '参与消化科著作编写']})
        self.assertEqual(summary['clinical'], '甲医院，主任医师')
        self.assertIn('科室', summary['missing'])

    def test_slide_objects_keep_geometry(self):
        templates = {s['number']: s for s in self.models['input1']['template']['slides']}
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            original = {s['id']: s for s in templates[spec['template_slide']]['shapes']}
            for shape in slide['shapes']:
                expected = original[shape['id']]['bbox']
                if spec.get('expert') and shape['id'] == templates[spec['template_slide']]['fields']['photo']:
                    # 照片对象按可见圆角框校正，其余模板对象保持原坐标。
                    frame = photo_frame_shape(templates[spec['template_slide']], original[shape['id']])
                    if frame:
                        expected = frame['bbox']
                self.assertEqual(shape['bbox'], expected)

    def test_replacement_photo_fills_frame(self):
        package = read_package(self.output)
        for slide, spec in zip(self.deck['slides'], self.report['pages']):
            if not spec.get('expert'):
                continue
            source = self.models['input1']['template']['slides'][spec['template_slide'] - 1]
            picture = next(s for s in slide['shapes'] if s['id'] == source['fields']['photo'])
            source_picture = next(s for s in source['shapes'] if s['id'] == source['fields']['photo'])
            frame = photo_frame_shape(source, source_picture)
            expected_bbox = frame['bbox'] if frame else source_picture['bbox']
            self.assertEqual(picture['bbox'], expected_bbox)
            _, _, width, height = expected_bbox
            with Image.open(io.BytesIO(package[picture['image']])) as image:
                # 替换后的照片尺寸直接匹配图片框比例，输出不再包含透明补边。
                self.assertAlmostEqual(image.width / image.height, width / height, places=2)
                self.assertEqual(image.mode, 'RGB')
            if source['fields'].get('role'):
                # 角色标签必须位于照片之后，确保PPT渲染时标签显示在照片上层。
                shape_ids = [shape['id'] for shape in slide['shapes']]
                self.assertLess(shape_ids.index(source['fields']['photo']),
                                shape_ids.index(source['fields']['role']))

    def test_template_names_and_ids_are_not_required(self):
        model = self.models['input1']
        package = read_package(model['template']['path'])
        for slide in model['template']['slides']:
            root = xml(package[slide['part']])
            for prop in root.findall('.//p:cNvPr', NS):
                prop.set('id', str(int(prop.get('id')) + 800))
                prop.set('name', '任意对象名称')
            package[slide['part']] = encoded(root)
        target = self.work / 'unseen.pptx'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, data in package.items():
                archive.writestr(name, data)
        detected = analyze_template(target)
        self.assertEqual([s['role'] for s in detected['slides']], [s['role'] for s in model['template']['slides']])
        self.assertGreater(int(detected['slides'][2]['fields']['identity']), 800)

    def test_draft_removes_old_photo(self):
        model = copy.deepcopy(self.models['input'])
        model['options']['draft'] = True
        path = self.work / 'draft.pptx'
        report = generate(model, path)
        deck = read_deck(path)
        pairs = [(s, p) for s, p in zip(deck['slides'], report['pages']) if p.get('expert') == '朱帅']
        self.assertEqual(len(pairs), 1)
        self.assertIn('简介待补充', pairs[0][0]['text'])
        self.assertEqual(len([s for s in pairs[0][0]['shapes'] if s['kind'] == 'pic']), 1)

    def test_time_overlap_blocks(self):
        model = copy.deepcopy(self.models['input1'])
        model['agenda']['events'][1]['time'] = '19:05-19:40'
        with self.assertRaisesRegex(ValueError, '重叠'):
            validate_model(model)

    def test_ambiguous_photo_requires_confirmation(self):
        model = copy.deepcopy(self.models['input1'])
        # 正式模式下仍需确认存在歧义的候选照片。
        model['options']['draft'] = False
        model['experts'][0]['photo_confirmed'] = False
        with self.assertRaisesRegex(ValueError, '候选照片'):
            validate_model(model)

    def test_duplicate_field_binding_blocks(self):
        model = copy.deepcopy(self.models['input1'])
        fields = model['template']['slides'][2]['fields']
        fields['bio'] = fields['identity']
        with self.assertRaisesRegex(ValueError, '同一区域'):
            validate_model(model)

    def test_template_profile_cache_reused(self):
        model = self.models['input1']
        cache = self.work / 'cache'
        cache.mkdir(exist_ok=True)
        mappings = [{'role': s['role'], 'fields': s['fields']} for s in model['template']['slides']]
        (cache / (model['template']['fingerprint'] + '.json')).write_text(json.dumps(mappings), encoding='utf-8')
        template, agenda, experts = discover(ROOT / 'input1')
        repeated = analyze(template, agenda, experts, self.work / 'cached', cache)
        self.assertTrue(repeated['template']['cache_used'])

    def test_incomplete_template_blocks(self):
        model = copy.deepcopy(self.models['input1'])
        model['template']['slides'][5]['fields'].pop('bio')
        with self.assertRaisesRegex(ValueError, 'bio'):
            page_plan(model)

    def test_client_cannot_replace_server_paths(self):
        original = self.models['input1']
        incoming = copy.deepcopy(original)
        incoming['experts'][0]['path'] = 'C:/private/file.docx'
        incoming['template']['path'] = 'C:/private/file.pptx'
        updated = apply_edits(original, incoming)
        self.assertEqual(updated['experts'][0]['path'], original['experts'][0]['path'])
        self.assertEqual(updated['template']['path'], original['template']['path'])

    def test_sources_unchanged(self):
        for path, checksum in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checksum)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_home_and_config(self):
        self.assertIn('AutoPPT', urllib.request.urlopen(self.url).read().decode())
        data = json.load(urllib.request.urlopen(self.url + '/api/config'))
        self.assertEqual(data['token'], TOKEN)

    def test_static_types_ignore_system_file_associations(self):
        # 模拟系统将扩展名关联为普通文本，脚本与样式仍须返回浏览器认可的类型。
        with patch('mimetypes.guess_type', return_value=('text/plain', None)):
            for path, mime in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')):
                with self.subTest(path=path), urllib.request.urlopen(self.url + path) as response:
                    self.assertEqual(response.headers['Content-Type'], mime + '; charset=utf-8')
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertTrue(response.read())

    def test_cross_site_post_blocked(self):
        request = urllib.request.Request(self.url + '/api/sample', data=b'{"name":"input1"}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)

    def test_directory_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + '/../../app.py')
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
