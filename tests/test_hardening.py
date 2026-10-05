import unittest
from music_companion.ai_client import _merge_with_local
from music_companion.recommender import recommend

class HardeningTests(unittest.TestCase):
 def test_ai_rejects_inverted_or_out_of_range_target(self):
  local=recommend({'mood':'平静'},ai_client=None)
  with self.assertRaises(Exception):
   _merge_with_local(local, {'recommendation': {'bpm_min':180,'bpm_max':100,'target_bpm':150,'genres':['x']}})
 def test_preferred_style_only_is_valid(self):
  result=recommend({'preferred_style':'电子'},ai_client=None)
  self.assertEqual(result['source'],'local')
if __name__=='__main__':unittest.main()
