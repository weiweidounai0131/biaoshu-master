#!/usr/bin/env python3
"""Lifecycle and limit tests for the final delivery confirmation API."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as bid_server


class Stage4LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name)
        self.data_dir = self.project_dir / bid_server.DATA_DIR_NAME
        self.data_dir.mkdir()
        self._write_stages()
        self.httpd = bid_server.BidConfirmServer(self.project_dir, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{bid_server.HOST}:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def _write(self, name: str, value: dict) -> None:
        bid_server.atomic_write_json(self.data_dir / name, value)

    def _receipt(self, name: str, value: dict) -> dict:
        value["confirmation_sha256"] = bid_server.sha256_data(value)
        self._write(name, value)
        return value

    def _write_stages(self) -> None:
        project_id = "stage4-test"
        stage1 = {"schema_version":1,"stage":"stage1","project_id":project_id,"project":{"project_name":"测试标书"},"scoring":{},"formatting":{"target_pages":20},"boundaries":{}}
        self._write(bid_server.STAGE1_INPUT, stage1)
        stage1_receipt = self._receipt(bid_server.STAGE1_RECEIPT,{"schema_version":1,"stage":"stage1","status":"confirmed","project_id":project_id,"source_sha256":bid_server.sha256_data(stage1),"data":stage1,"confirmed_at":bid_server.utc_now()})
        chapters=[]
        for index in range(1,3):
            chapters.append({"id":f"chapter-{index}","number":str(index),"title":f"第{index}章","level":1,"order":index,"pages":10,"score_refs":[],"requirement_refs":[],"allow_deeper":False,"children":[]})
        stage2={"schema_version":1,"stage":"stage2","project_id":project_id,"stage1_confirmation_sha256":stage1_receipt["confirmation_sha256"],"target_pages":20,"coverage":{"total":0,"mapped":0,"unmapped":[]},"chapters":chapters}
        self._write(bid_server.STAGE2_INPUT,stage2)
        stage2_receipt=self._receipt(bid_server.STAGE2_RECEIPT,{"schema_version":1,"stage":"stage2","status":"confirmed","project_id":project_id,"stage1_confirmation_sha256":stage1_receipt["confirmation_sha256"],"source_sha256":bid_server.sha256_data(stage2),"data":{"chapters":chapters,"coverage":stage2["coverage"],"planned_pages":20},"confirmed_at":bid_server.utc_now()})
        image={"id":"image-1","figure_no":"图1-1","order":1,"chapter_id":"chapter-1","chapter_number":"1","chapter_title":"第1章","position":{"outline_node_id":"chapter-1","outline_number":"1","outline_title":"第1章","placement_note":"章导语后"},"name":"总览图","type":"章首总览图","purpose":"概括方案","core_nodes":["目标"],"composition":"分层结构","orientation":"landscape","is_chapter_overview":True,"origin":"ai"}
        settings=[{"chapter_id":"chapter-1","chapter_number":"1","chapter_title":"第1章","overview_policy":"required","overview_reason":"需要总览"},{"chapter_id":"chapter-2","chapter_number":"2","chapter_title":"第2章","overview_policy":"exempt","overview_reason":"无需图片"}]
        stage3={"schema_version":1,"stage":"stage3","project_id":project_id,"stage2_confirmation_sha256":stage2_receipt["confirmation_sha256"],"visual_direction":{"palette":"深蓝、红色与白色","style":"商务","background":"白色或浅灰底","density":"适中","avoid":["复杂渐变"]},"chapter_settings":settings,"images":[image],"cleanup_actions":[]}
        self._write(bid_server.STAGE3_INPUT,stage3)
        self.stage3_receipt=self._receipt(bid_server.STAGE3_RECEIPT,{"schema_version":1,"stage":"stage3","status":"confirmed","project_id":project_id,"stage2_confirmation_sha256":stage2_receipt["confirmation_sha256"],"source_sha256":bid_server.sha256_data(stage3),"data":{"visual_direction":stage3["visual_direction"],"chapter_settings":settings,"images":[image],"cleanup_actions":[]},"confirmed_at":bid_server.utc_now()})
        delivery={"word_batch_count":2,"word_batches":[{"id":"word-batch-1","order":1,"chapter_ids":["chapter-1"],"chapter_numbers":["1"],"chapter_titles":["第1章"],"planned_pages":10,"output_filename":"测试标书-第1批-第1章.docx"},{"id":"word-batch-2","order":2,"chapter_ids":["chapter-2"],"chapter_numbers":["2"],"chapter_titles":["第2章"],"planned_pages":10,"output_filename":"测试标书-第2批-第2章.docx"}],"image_plan_workbook":{"count":1,"format":".xlsx","filename":"测试标书-图片规划表.xlsx","purpose":"交给其他AI生图","worksheet_names":["图片规划清单"],"columns":["图号"],"image_count":1},"skill_boundary":{"generate_word_documents":True,"generate_image_plan_excel":True,"generate_images":False,"insert_images":False},"delivery_output_dir":str(self.project_dir),"additional_notes":""}
        self.delivery=delivery
        self.stage4={"schema_version":1,"stage":"stage4","project_id":project_id,"generated_at":bid_server.utc_now(),"stage3_confirmation_sha256":self.stage3_receipt["confirmation_sha256"],"summary":{"project_name":"测试标书","client":"","project_overview":"","chapter_count":2,"planned_pages":20,"image_count":1},"delivery":delivery}
        self._write(bid_server.STAGE4_INPUT,self.stage4)

    def request(self,path:str,data:dict|None=None)->tuple[int,dict]:
        body=None if data is None else json.dumps(data,ensure_ascii=False).encode()
        request=urllib.request.Request(self.base_url+path,data=body,method="GET" if data is None else "POST",headers={"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(request,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            try:return error.code,json.load(error)
            finally:error.close()

    def test_limits_and_lifecycle(self) -> None:
        status,payload=self.request('/api/stage4');self.assertEqual(status,200);self.assertEqual(payload['workflow']['active_stage'],'stage4')
        status,rules=self.request('/api/generation-rules');self.assertEqual(status,200);self.assertEqual(rules['generation_rules']['default_profile_id'],'default');self.assertGreaterEqual(len(rules['generation_rules']['profiles']),4)
        preset=next(item for item in rules['generation_rules']['profiles'] if item['id']=='system-integration-delivery')
        self.delivery['generation_rule']={key:preset[key] for key in ('id','name','kind','description','path','sha256','base_id','base_sha256','effective_sha256')}
        source_hash=payload['source_sha256']
        for invalid_count in (0,6,1.5):
            invalid=deepcopy(self.delivery);invalid['word_batch_count']=invalid_count
            status,result=self.request('/api/stage4/confirm',{'source_sha256':source_hash,'data':invalid})
            self.assertEqual(status,422);self.assertFalse(result['ok'])
        status,result=self.request('/api/stage4/confirm',{'source_sha256':source_hash,'data':self.delivery});self.assertEqual(status,200);self.assertEqual(result['receipt']['status'],'confirmed')
        self.assertEqual(result['receipt']['data']['generation_rule']['id'],'system-integration-delivery')
        self.assertEqual(result['receipt']['data']['word_batch_count'],2);self.assertFalse(result['receipt']['data']['skill_boundary']['generate_images'])
        status,duplicate=self.request('/api/stage4/confirm',{'source_sha256':source_hash,'data':self.delivery});self.assertEqual(status,422);self.assertFalse(duplicate['ok'])
        status,reopened=self.request('/api/stage4/reopen',{});self.assertEqual(status,200);self.assertEqual(reopened['mode'],'editing')
        status,editing=self.request('/api/stage4');self.assertEqual(status,200);self.assertEqual(editing['draft']['data']['word_batch_count'],self.delivery['word_batch_count']);self.assertEqual(Path(editing['draft']['data']['delivery_output_dir']).resolve(),self.project_dir.resolve())
        status,reconfirmed=self.request('/api/stage4/confirm',{'source_sha256':source_hash,'data':self.delivery});self.assertEqual(status,200)
        self.assertEqual(bid_server.wait_for_stage(self.project_dir,'stage4',1),0)

    def test_delivery_status_and_upstream_reopen_archive_active_delivery(self) -> None:
        source_hash = bid_server.sha256_data(self.stage4)
        status, confirmed = self.request('/api/stage4/confirm', {'source_sha256': source_hash, 'data': self.delivery})
        self.assertEqual(status, 200)
        delivery_dir = self.project_dir / bid_server.DELIVERY_DIR_NAME
        delivery_dir.mkdir()
        bid_server.atomic_write_json(delivery_dir / 'manifest.json', {
            'stage4_confirmation_sha256': confirmed['receipt']['confirmation_sha256'],
        })
        status, delivery = self.request('/api/delivery-status')
        self.assertEqual(status, 200)
        self.assertTrue(delivery['delivery_ready'])
        status, reopened = self.request('/api/stage2/reopen', {})
        self.assertEqual(status, 200)
        self.assertIsNotNone(reopened['archived_delivery'])
        self.assertFalse(delivery_dir.exists())
        self.assertTrue(Path(reopened['archived_delivery']).is_dir())


if __name__=="__main__":unittest.main()
