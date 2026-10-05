import unittest
from music_companion.recommender import recommend
class ContextTests(unittest.TestCase):
 def test_plan_context_drives_bpm_and_explanation(self):
  r=recommend({'schedule':'学习','plan_context':{'plan_type':'walk','target_bpm':96,'reason':'压力高后的恢复散步'}},ai_client=None)
  self.assertEqual(r['recommendation']['target_bpm'],96)
  self.assertIn('恢复散步',r['recommendation']['rationale'])
if __name__=='__main__':unittest.main()
