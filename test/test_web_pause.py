import signal
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import web_app


def make_job(job_id, status, paused_from=None, enqueued=False):
    return {
        "id": job_id,
        "status": status,
        "mode": "logo-lama",
        "mode_label": "Fixed watermark LAMA",
        "filename": "input.mp4",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "progress": 0,
        "message": "queued",
        "logs": [],
        "error": None,
        "paused_from": paused_from,
        "enqueued": enqueued,
    }


class FakeProcess:
    pid = 12345

    @staticmethod
    def poll():
        return None


class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        with web_app._jobs_lock:
            web_app._jobs.clear()
        with web_app._processes_lock:
            web_app._processes.clear()
        while True:
            try:
                web_app._job_queue.get_nowait()
                web_app._job_queue.task_done()
            except web_app.queue.Empty:
                break

    def test_queued_job_can_pause_and_resume(self):
        web_app._jobs["queued"] = make_job("queued", "queued")

        paused = web_app.pause_job("queued")
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(paused["can_resume"])

        resumed = web_app.resume_job("queued")
        self.assertEqual(resumed["status"], "queued")
        self.assertTrue(resumed["can_pause"])
        self.assertEqual(web_app._job_queue.get_nowait(), "queued")
        web_app._job_queue.task_done()

    def test_queued_job_with_existing_token_is_not_duplicated(self):
        web_app._jobs["queued"] = make_job("queued", "queued", enqueued=True)
        web_app._job_queue.put_nowait("queued")

        web_app.pause_job("queued")
        web_app.resume_job("queued")

        self.assertEqual(web_app._job_queue.qsize(), 1)
        self.assertEqual(web_app._job_queue.get_nowait(), "queued")
        web_app._job_queue.task_done()

    @patch.object(web_app.os, "killpg")
    def test_processing_job_signals_process_group(self, killpg):
        web_app._jobs["running"] = make_job("running", "processing")
        web_app._processes["running"] = FakeProcess()

        web_app.pause_job("running")
        web_app.resume_job("running")

        self.assertEqual(
            killpg.call_args_list,
            [unittest.mock.call(12345, signal.SIGSTOP), unittest.mock.call(12345, signal.SIGCONT)],
        )

    def test_completed_job_cannot_pause(self):
        web_app._jobs["done"] = make_job("done", "succeeded")

        with self.assertRaises(HTTPException) as raised:
            web_app.pause_job("done")
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
