from concurrent.futures import ThreadPoolExecutor


executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deploy-console")


def submit_background_job(func, *args, **kwargs):
    return executor.submit(func, *args, **kwargs)
