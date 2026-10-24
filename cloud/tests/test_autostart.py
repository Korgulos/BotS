import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import fcntl

from swgoh_cloud.bridge import run

spec = importlib.util.spec_from_file_location('watch_game', Path(__file__).parents[1]/'watch-game.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def test_game_detection_uses_executable_not_shell_arguments(tmp_path):
    for pid, name, argv, state in [(100, 'SWGoH.exe', b'C:\\Games\\SWGoH.exe\0', 'S'),
                                  (101, 'bash', b'bash\0-c\0echo SWGoH.exe\0', 'S'),
                                  (102, 'SWGoH.exe', b'SWGoH.exe\0', 'Z')]:
        p=tmp_path/str(pid);p.mkdir()
        (p/'comm').write_text(name)
        (p/'cmdline').write_bytes(argv)
        (p/'stat').write_text(f'{pid} ({name}) '+ ' '.join([state]+['0']*18+['12345']))
    assert watch.game_processes(tmp_path)=={(100,'12345')}


def test_lifecycle_waits_20_seconds_opens_once_and_stops_bridge(tmp_path,monkeypatch):
    root=tmp_path;config=root/'config.json';config.write_text(json.dumps({'inspector_url_file':'unused'}))
    opener=root/'open.py';opener.write_text('')
    ticks=[0];starts=[];opens=[];stops=[]
    watch.STOP=False
    def sleep(_):
        ticks[0]+=1
        if ticks[0]>27:watch.STOP=True
    monkeypatch.setattr(watch,'time',SimpleNamespace(monotonic=lambda:ticks[0],sleep=sleep))
    monkeypatch.setattr(watch,'game_processes',lambda:{(100,'1')} if ticks[0]<26 else set())
    monkeypatch.setattr(watch,'ready',lambda _:ticks[0]>=23)
    class Child:
        dead=False
        def poll(self):return 0 if self.dead else None
        def terminate(self):self.dead=True;stops.append(ticks[0])
        def wait(self,timeout=None):return 0
    def start(*args,**kwargs):starts.append(ticks[0]);return Child()
    def open_ui(*args,**kwargs):opens.append(ticks[0]);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(watch.subprocess,'Popen',start)
    monkeypatch.setattr(watch.subprocess,'run',open_ui)
    watch.watch(20,opener,config,root)
    assert starts==[20] and opens==[23] and stops==[26]


def test_manual_and_auto_bridge_share_lock(tmp_path,capsys):
    path=str(tmp_path/'outbox.db')
    with open(path+'.bridge.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run({'outbox_path':path},once=True)
    assert 'already running' in capsys.readouterr().out
    assert not Path(path).exists()
