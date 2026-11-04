"""Additional boundary tests beyond the public platform samples."""
import json
from pathlib import Path
import tempfile
import unittest
from backend import Backend, InputError, MAX_TIME, load_case, write_json


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'case.json'
        self.case = {'report_horizon_s':3600,'stations':{'S1':{'cycle_s':40}},'jobs':[
            {'job_id':'late','station_id':'S1','ready_s':1000},
            {'job_id':'early','station_id':'S1','ready_s':0}]}
        write_json(self.path,self.case)
        self.backend = Backend(self.path,self.root/'state.db')

    def tearDown(self):
        self.backend.close()
        self.tmp.cleanup()

    def test_gap_and_state_boundaries(self):
        late = self.backend.handle({'op':'submit','job':self.case['jobs'][0]})['run']
        early = self.backend.handle({'op':'submit','job':self.case['jobs'][1]})['run']
        self.assertEqual((early['start_s'],late['start_s']),(0,1000))
        for at,state in [(0,'not_ready'),(999,'not_ready'),(1000,'running'),(1039,'running'),(1040,'simulated_completed')]:
            self.assertEqual(self.backend.handle({'op':'get','job_id':'late','at_s':at})['state'],state)

    def test_invalid_times_and_shapes_do_not_write_events(self):
        before = self.backend.handle({'op':'report'})
        for bad in (True,1.0,-1,MAX_TIME+1,'0',None):
            self.assertEqual(self.backend.handle({'op':'report','at_s':bad})['status'],'error')
            self.assertEqual(self.backend.handle({'op':'submit','job':dict(self.case['jobs'][0],ready_s=bad)})['status'],'error')
        for bad in (None,[],{}, {'op':[]}, {'op':'submit'}, {'op':'get','job_id':[]}, {'op':'report','extra':1}):
            self.assertEqual(self.backend.handle(bad)['status'],'error')
        self.assertEqual(before,self.backend.handle({'op':'report'}))

    def test_conflict_before_acceptance_and_unknown_job(self):
        altered = dict(self.case['jobs'][0],ready_s=999)
        self.assertEqual(self.backend.handle({'op':'submit','job':altered})['status'],'conflict')
        self.assertEqual(self.backend.handle({'op':'get','job_id':'late'})['status'],'not_found')
        self.assertEqual(self.backend.handle({'op':'submit','job':dict(altered,job_id='unknown')})['code'],'unknown_job')
        report = self.backend.handle({'op':'report'})
        self.assertEqual(report['metrics']['event_counts'],{'conflict':1})
        self.assertEqual(report['runs'],[])

    def test_case_binding_rejects_changes(self):
        self.case['stations']['S1']['cycle_s'] = 41
        write_json(self.path,self.case)
        with self.assertRaisesRegex(InputError,'different batch'):
            Backend(self.path,self.root/'state.db')

    def test_input_case_conflict_and_integer_limits(self):
        for mutate in ('conflict','cycle_bool','horizon_zero','ready_overflow','end_overflow','shared_resource'):
            case = json.loads(json.dumps(self.case))
            if mutate == 'conflict':
                case['jobs'].append(dict(case['jobs'][0],ready_s=1))
            elif mutate == 'cycle_bool':
                case['stations']['S1']['cycle_s'] = True
            elif mutate == 'horizon_zero':
                case['report_horizon_s'] = 0
            elif mutate == 'ready_overflow':
                case['jobs'][0]['ready_s'] = MAX_TIME+1
            elif mutate == 'end_overflow':
                case['jobs'][0]['ready_s'] = MAX_TIME
            else:
                case['stations']['S1']['pump_id'] = 'P1'
            write_json(self.path,case)
            with self.subTest(mutate=mutate), self.assertRaises(InputError):
                load_case(self.path)

    def test_reservation_overflow_rolls_back_and_max_end_is_valid(self):
        case = {'report_horizon_s':MAX_TIME,'stations':{'S1':{'cycle_s':1}},'jobs':[
            {'job_id':'a','station_id':'S1','ready_s':MAX_TIME-1},
            {'job_id':'b','station_id':'S1','ready_s':MAX_TIME-1}]}
        write_json(self.path,case)
        other = Backend(self.path,self.root/'limit.db')
        try:
            self.assertEqual(other.handle({'op':'submit','job':case['jobs'][0]})['run']['end_s'],MAX_TIME)
            before = other.handle({'op':'report','at_s':MAX_TIME})
            failed = other.handle({'op':'submit','job':case['jobs'][1]})
            self.assertEqual(failed['code'],'time_overflow')
            self.assertEqual(before,other.handle({'op':'report','at_s':MAX_TIME}))
            self.assertEqual(before['metrics']['simulated_completed_count'],1)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
