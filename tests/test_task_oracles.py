import json
import unittest
from fenjue.research.tasks import build_suite, grade, public_task, execute_tool, _SQL_SCREEN, _SQL_CONFIRM

class TaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.suite = build_suite(20261007)
    def test_exact_correct_and_wrong(self):
        for task in self.suite['tasks']:
            if task['grader']['kind'] == 'exact_json':
                self.assertTrue(grade(task, json.dumps(task['grader']['expected']))['passed'])
                self.assertFalse(grade(task, '{}')['passed'])
    def test_public_projection_has_no_grader(self):
        self.assertTrue(all('grader' not in public_task(t) for t in self.suite['tasks']))
    def test_sql_rejects_write_and_multiple_statements(self):
        task = next(t for t in self.suite['tasks'] if t['category']=='coding')
        for query in ['DROP TABLE orders', 'SELECT 1; SELECT 2', "SELECT load_extension('bad')"]:
            self.assertFalse(grade(task,json.dumps({'query':query}))['passed'])
    def test_all_sql_reference_answers_pass_restricted_grader(self):
        for split, specs in (('screen',_SQL_SCREEN[:3]),('confirm',_SQL_CONFIRM[:8])):
            for index,spec in enumerate(specs):
                task=next(t for t in self.suite['tasks'] if t['id']==f'{split}-coding-{index+1:02d}')
                self.assertTrue(grade(task,json.dumps({'query':spec[3]}))['passed'],task['id'])
    def test_tools_require_observed_reads(self):
        task=next(t for t in self.suite['tasks'] if t['category']=='tools')
        answer=json.dumps(task['grader']['expected'])
        self.assertFalse(grade(task,answer)['passed'])
        state={}
        for rid in task['grader']['required_record_ids']:
            execute_tool(task,'fixture.read_record',{'record_id':rid},state)
        self.assertTrue(grade(task,answer,state['tool_trace'])['passed'])
        self.assertFalse(execute_tool(task,'shell',{'command':'whoami'},state)['ok'])
