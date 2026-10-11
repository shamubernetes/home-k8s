import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import kopiur_pds_original as original


class OriginalTests(unittest.TestCase):
    def payload(self):
        return {'original':{'snapshots':{'nas':'a'*32,'r2':'b'*32},
            'bundle_sha256':{name:'c'*64 for name in ('application-state.tar','runtime.json','metadata.json')},
            'runtime_sha256':'d'*64}}

    def test_exact_distinct_ids(self):
        for field,value in [('nas','A'*32),('r2','a'*32),('r2','b'*31)]:
            payload=self.payload();payload['original']['snapshots'][field]=value
            with self.assertRaises(ValueError):original.validate_original(payload)
        self.assertEqual(original.validate_original(self.payload())['snapshots']['nas'],'a'*32)

    def test_contract_rejects_unexpected_fields_and_missing_hashes(self):
        payload=self.payload();payload['original']['destination']='other'
        with self.assertRaises(ValueError):original.validate_original(payload)
        payload=self.payload();del payload['original']['bundle_sha256']['runtime.json']
        with self.assertRaises(ValueError):original.validate_original(payload)

    def test_owned_memory_volume(self):
        drill=original.Original()
        with mock.patch.object(original.native,'docker',return_value='') as docker:
            name=drill.volume('nas')
        self.assertEqual(drill.volumes,[name])
        args=docker.call_args_list[0].args
        self.assertIn('type=tmpfs',args)
        keeper=docker.call_args_list[1].args
        self.assertEqual(keeper[keeper.index('--network')+1],'none')
        self.assertEqual(keeper[keeper.index('--user')+1],'1000:1000')
        self.assertEqual(keeper[keeper.index('--log-driver')+1],'none')
        self.assertIn(name+':/memory',keeper)
        self.assertEqual(len(drill.containers),1)
        self.assertNotIn('--cap-add',keeper)
        self.assertIn('o=size=192m,uid=1000,gid=1000,mode=0700',args)
        self.assertIn('k8s92.pds='+drill.prefix,args)

    def extract(self, mutation=None, wrong_hash=False, reordered=False,
                directory_uid=1000, file_uid=1000):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            (source/'key').write_bytes(b'x'*32)
            for name in ('account','sequencer','actor'):
                c=sqlite3.connect(source/(name+'.sqlite'))
                c.execute('create table records(id integer primary key,value text)')
                c.execute('insert into records values(1,?)',('original',));c.commit();c.close()
            bundle=root/'restored/.kopiur-pds/current';bundle.mkdir(parents=True)
            (bundle.parent/'COMPLETE').write_text('complete\n')
            with tarfile.open(bundle/'application-state.tar','w') as archive:
                for path in [source,*sorted(source.iterdir())]:
                    info=archive.gettarinfo(str(path),arcname='.' if path==source else './'+path.name)
                    info.gid=1000
                    info.uid=directory_uid if path.is_dir() else file_uid
                    if path.is_dir():archive.addfile(info)
                    else:
                        with path.open('rb') as f:archive.addfile(info,f)
                if mutation:
                    info=tarfile.TarInfo(mutation);info.uid=info.gid=1000;info.size=1
                    archive.addfile(info,io.BytesIO(b'x'))
            (bundle/'runtime.json').write_text('{}')
            (bundle/'metadata.json').write_text(json.dumps({'format':1,'contract':'stopped-pds-whole-store'}))
            expected={name:hashlib.sha256((bundle/name).read_bytes()).hexdigest()
                      for name in ('application-state.tar','runtime.json','metadata.json')}
            (bundle/'SHA256SUMS').write_text(''.join(expected[n]+'  '+n+'\n' for n in expected))
            if wrong_hash:expected['runtime.json']='a'*64
            if reordered:expected=dict(reversed(list(expected.items())))
            # Replace only the two explicit container-root paths, not fixture input.
            code=original.EXTRACT.replace("'/data/restored/.kopiur-pds'",repr(str(root/'restored/.kopiur-pds')))
            code=code.replace("'/data/point'",repr(str(root/'point')))
            # Host regressions cannot chown. The real isolated ARC extraction
            # retains VERIFY_METADATA=True and proves native UID/GID/mode equality.
            code=code.replace('VERIFY_METADATA=True','VERIFY_METADATA=False')
            return subprocess.run([sys.executable,'-c',code],input=json.dumps(expected),
                                  capture_output=True,text=True,timeout=10)

    def test_real_sqlite_bundle_extraction(self):
        result=self.extract();self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout)
        self.assertEqual(len(receipt['databases']),3)
        self.assertEqual(receipt['actor_keys'],1)
        self.assertTrue(all(d['tables']['records']['rows']==1 for d in receipt['databases']))

    def test_root_owned_directories_allowed_but_root_files_denied(self):
        result=self.extract(directory_uid=0)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotEqual(self.extract(file_uid=0).returncode,0)
        self.assertNotEqual(self.extract(directory_uid=123).returncode,0)

    def test_metadata_checker_has_only_required_owned_tmpfs_capabilities(self):
        drill=original.Original()
        with mock.patch.object(original.native,'docker',return_value='') as docker:
            drill.start('check',original.native.PYTHON,{'owned':'/data'},
                        user='0:1000',capabilities=('CHOWN','FOWNER','DAC_OVERRIDE'))
        args=docker.call_args.args
        self.assertEqual(args[args.index('--network')+1],'none')
        self.assertEqual(args[args.index('--user')+1],'0:1000')
        self.assertEqual(args[args.index('--cap-drop')+1],'ALL')
        self.assertEqual({args[i+1] for i,a in enumerate(args) if a=='--cap-add'},
                         {'CHOWN','FOWNER','DAC_OVERRIDE'})
        self.assertNotIn('--privileged',args)
        self.assertIn('VERIFY_METADATA=True',original.EXTRACT)

    def test_unsafe_and_duplicate_members_denied(self):
        for name in ('../escape','/absolute','./account.sqlite'):
            with self.subTest(name=name):self.assertNotEqual(self.extract(name).returncode,0)

    def test_bundle_hash_mismatch_denied(self):
        self.assertNotEqual(self.extract(wrong_hash=True).returncode,0)

    def test_checksum_payload_order_is_irrelevant(self):
        result=self.extract(reordered=True);self.assertEqual(result.returncode,0,result.stderr)

    def test_absolute_command_deadline(self):
        token=original.native.COMMAND_DEADLINE.set(10)
        try:
            with mock.patch.object(original.native.time,'monotonic',return_value=11), \
                    mock.patch.object(original.native.subprocess,'run') as run:
                with self.assertRaises(TimeoutError):original.native.docker('pull','image')
                run.assert_not_called()
            with mock.patch.object(original.native.time,'monotonic',return_value=8), \
                    mock.patch.object(original.native.subprocess,'run',return_value=mock.Mock(returncode=0,stdout='')) as run:
                original.native.docker('pull','image',timeout=600)
                self.assertEqual(run.call_args.kwargs['timeout'],2)
        finally:original.native.COMMAND_DEADLINE.reset(token)

    def test_original_containers_disable_log_persistence(self):
        drill=original.Original()
        with mock.patch.object(original.native,'docker',return_value='') as docker:
            drill.start('boot','image',{},command=['node','index.ts'])
        args=docker.call_args.args
        self.assertIn('--log-driver',args)
        self.assertEqual(args[args.index('--log-driver')+1],'none')
        self.assertNotIn('-e',args)

    def test_failed_original_boot_never_requests_persisted_logs(self):
        drill=original.Original()
        with mock.patch.object(drill,'http',side_effect=RuntimeError('not ready')), \
                mock.patch.object(original.native,'docker',return_value='false'), \
                mock.patch.object(original.native.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'native logs disabled'):
                drill.ready('owned','/xrpc/_health')
            run.assert_not_called()

    def test_reader_never_calls_repository_mutation(self):
        source=Path(original.__file__).read_text()
        for forbidden in ('repository create','snapshot create','snapshot delete','rclone purge','transport.cleanup('):
            self.assertNotIn(forbidden,source)
        self.assertIn('--readonly',source)
        self.assertIn('networked_reader_removed_before_boot=True',source)
        self.assertIn('drill.clean()',source)


if __name__=='__main__':unittest.main()
