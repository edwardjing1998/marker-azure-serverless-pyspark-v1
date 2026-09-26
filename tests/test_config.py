from worker.config import Config, route_document


def test_gpu_config_local_paths():
    cfg = Config('/work/source', '/work/generated', '/work/state', request_id='a'*32, compute_mode='gpu').validate()
    assert route_document(cfg, pages=1, size=100) == ('gpu', 'requested_gpu')
