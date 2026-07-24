from worker.rescore import rescore_pending_jobs
from profile.models import UserProfile


class _Job:
    def __init__(self, title):
        self.title = title; self.company = ""; self.location = ""
        self.employment_type = ""; self.seniority = ""; self.description = ""
        self.requirements = ""; self.apply_url = ""; self.source_url = "x"
        self.date_posted = ""; self.keywords = None
        self.status = None; self.score = None


class _Query:
    def __init__(self, jobs): self._jobs = jobs
    def filter(self, *a, **k): return self
    def all(self): return self._jobs


class _DB:
    def __init__(self, jobs): self._jobs = jobs; self.committed = False
    def query(self, *a, **k): return _Query(self._jobs)
    def commit(self): self.committed = True


def test_rescore_updates_scores():
    from db.models import JobStatus
    jobs = [_Job("RF Engineer")]
    jobs[0].status = JobStatus.SCORED
    prof = UserProfile(); prof.preferences.roles = ["RF Engineer"]
    n = rescore_pending_jobs(_DB(jobs), prof)
    assert n == 1
    assert jobs[0].score is not None
