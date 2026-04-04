from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread


executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deploy-console")
_scheduler_stop_event = Event()
_scheduler_thread: Thread | None = None


def submit_background_job(func, *args, **kwargs):
    return executor.submit(func, *args, **kwargs)


def start_background_scheduler(target, *, name: str = "deploy-console-scheduler"):
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return _scheduler_thread
    _scheduler_stop_event.clear()
    _scheduler_thread = Thread(target=target, args=(_scheduler_stop_event,), daemon=True, name=name)
    _scheduler_thread.start()
    return _scheduler_thread


def stop_background_scheduler():
    _scheduler_stop_event.set()
