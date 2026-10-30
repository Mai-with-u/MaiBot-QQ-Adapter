import json,sqlite3,unittest
from reference_context import history_author_candidate as match

class HistoryMatchTests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.execute('create table refs(scope,alias,record,created)')
  self.payload={'id':'current','msg_elements':[{'msg_idx':'quoted-index','content':'1'}]}
 def tearDown(self):self.db.close()
 def put(self,mid,uid,text='1',scope='account/group',created=1000):
  record={'id':mid,'author':{'member_openid':uid,'username':'name'},'content':text}
  self.db.execute('insert into refs values(?,?,?,?)',(scope,mid,json.dumps(record),created))
 def result(self):return match(self.db,'account/group',self.payload,'quoted-index',now=1001)
 def test_unique_candidate_not_platform_confirmation(self):
  self.put('one','alice');r=self.result();self.assertEqual(r['author']['member_openid'],'alice');self.assertFalse(r['platform_confirmed']);self.assertNotIn('message_id',r)
 def test_two_authors_ambiguous_even_short_text(self):
  self.put('one','alice');self.put('two','bob');self.assertEqual(self.result()['status'],'ambiguous_authors')
 def test_same_author_repeated_has_no_reply_target(self):
  self.put('one','alice');self.put('two','alice');r=self.result();self.assertEqual(r['matching_message_count'],2);self.assertNotIn('message_id',r)
 def test_cross_account_group_isolation(self):
  self.put('one','alice',scope='other/group');self.assertEqual(self.result()['status'],'not_found')
 def test_complete_text_not_substring(self):
  self.put('one','alice',text='123');self.assertEqual(self.result()['status'],'not_found')
 def test_no_unanchored_element(self):
  self.put('one','alice');self.payload['msg_elements'][0]['msg_idx']='other';self.assertEqual(self.result()['status'],'unavailable')
 def test_current_message_is_not_source(self):
  self.put('current','alice');self.assertEqual(self.result()['status'],'not_found')
 def test_not_future_or_expired(self):
  self.put('future','alice',created=2000);self.put('expired','bob',created=-3000000);self.assertEqual(self.result()['status'],'not_found')
 def test_same_name_different_identity_remains_ambiguous(self):
  self.put('one','alice');self.put('two','bob');self.assertEqual(self.result()['author_count'],2)
 def test_forwarded_or_nested_content_not_parsed(self):
  self.payload['msg_elements'][0]['content']='=== 消息 1 ===';self.assertEqual(self.result()['status'],'unsupported_content')

