import unittest,tempfile,json,sqlite3
from contextlib import closing
from pathlib import Path
from reference_context import cache_reference as cache, reference_fields as fields
class IndexTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'index.db'
 def tearDown(self):self.tmp.cleanup()
 def original(self,idx='INDEX==',mid='original'):
  return {'id':mid,'author':{'nickname':'Alice','member_openid':'author-a'},'content':'same text','message_scene':{'ext':['msg_idx='+idx,'auth_token=PRIVATE']}}
 def quote(self,idx='INDEX=='):
  return {'id':'question','author':{'nickname':'Bob'},'content':'who said it','message_scene':{'ext':['msg_idx=QUESTION','ref_msg_idx='+idx]}}
 def test_exact_original_author(self):
  cache(self.db,'account/group',self.original());x=cache(self.db,'account/group',self.quote());self.assertEqual(x['msg_elements'][0]['author']['nickname'],'Alice')
 def test_scope_isolation(self):
  cache(self.db,'a/group',self.original());self.assertNotIn('message_reference',cache(self.db,'b/group',self.quote()))
 def test_no_text_matching(self):
  cache(self.db,'a',self.original());x=cache(self.db,'a',self.quote('MISSING'));self.assertTrue(x['_qq_reference_unresolved'])
 def test_ref_is_not_own_alias(self):
  cache(self.db,'a',self.quote());self.assertTrue(cache(self.db,'a',self.quote())['_qq_reference_unresolved'])
 def test_padding_and_secret(self):
  self.assertEqual(fields(self.original())['msg_idx'],'INDEX==');cache(self.db,'a',self.original());self.assertNotIn(b'PRIVATE',self.db.read_bytes())
 def test_sent_receipt(self):
  cache(self.db,'a',{},({'id':'bot-message','author':{'nickname':'Bot'},'content':'reply'},['bot-message','SENT==']));self.assertEqual(cache(self.db,'a',self.quote('SENT=='))['msg_elements'][0]['author']['nickname'],'Bot')
 def test_collision_unknown(self):
  cache(self.db,'a',self.original());cache(self.db,'a',self.original(mid='different'));self.assertTrue(cache(self.db,'a',self.quote())['_qq_reference_unresolved'])
 def test_direct_id(self):
  cache(self.db,'a',self.original());x=cache(self.db,'a',{'id':'q','author':{},'message_reference':{'message_id':'original'}});self.assertEqual(x['msg_elements'][0]['id'],'original')
 def test_unknown_reference_does_not_use_element_body_as_proof(self):
  cache(self.db,'a',self.original());q=self.quote('OTHER');q['msg_elements']=[{'content':'same text','msg_idx':'INDEX=='}]
  self.assertTrue(cache(self.db,'a',q)['_qq_reference_unresolved'])

