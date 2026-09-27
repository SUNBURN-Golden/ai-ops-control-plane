import base64
import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('migration', pathlib.Path(__file__).resolve().parents[1] / 'tools/extract_control_plane.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

REPO = m.SOURCE
OTHER_REPO = m.PRODUCTS[1]

def entry_for(data, path='scripts/control_plane.py'):
    return dict(path=path, sha=m.blob_sha(data), mode='100644', type='blob')

def blob_response(data, encoding='base64'):
    return {'sha': m.blob_sha(data), 'encoding': encoding,
            'content': base64.b64encode(data).decode()}

def blob_path(repo, sha):
    return f'repos/{repo}/git/blobs/{sha}'

class GitHubBlobReadTests(unittest.TestCase):
    # GitHub.read fetches repos/{repo}/git/blobs/{sha}, requires base64
    # encoding, verifies the git blob digest, then caches on (repo, sha).
    # Every response below is mocked; no network or gh calls occur.

    def test_valid_base64_returns_exact_bytes(self):
        for data in [b'', 'héllo wörld / 한국어\n'.encode('utf-8'), bytes(range(256))]:
            gh = m.GitHub()
            entry = entry_for(data)
            with patch.object(gh, 'api', return_value=blob_response(data)) as api:
                self.assertEqual(gh.read(REPO, entry), data)
            api.assert_called_once_with(blob_path(REPO, entry['sha']))

    def test_unsupported_encoding_rejected(self):
        data = b'payload\n'
        entry = entry_for(data)
        responses = [
            {'sha': entry['sha'], 'encoding': 'utf-8', 'content': 'payload\n'},
            {'sha': entry['sha'], 'encoding': 'none', 'content': 'payload\n'},
            {'sha': entry['sha'], 'content': base64.b64encode(data).decode()},
        ]
        for raw in responses:
            gh = m.GitHub()
            with patch.object(gh, 'api', return_value=raw):
                with self.assertRaises(m.MigrationError):
                    gh.read(REPO, entry)
            self.assertEqual(gh.cache, {})

    def test_digest_mismatch_rejected_never_cached_then_fresh_read(self):
        good, forged = b'real contents\n', b'forged contents\n'
        entry = entry_for(good)
        gh = m.GitHub()
        api = Mock(side_effect=[blob_response(forged), blob_response(good)])
        with patch.object(gh, 'api', api):
            with self.assertRaisesRegex(m.MigrationError, 'hash mismatch'):
                gh.read(REPO, entry)
            self.assertNotIn((REPO, entry['sha']), gh.cache)
            # A later valid response for the same key performs a new API read.
            self.assertEqual(gh.read(REPO, entry), good)
        self.assertEqual(api.call_count, 2)
        api.assert_called_with(blob_path(REPO, entry['sha']))

    def test_repeated_read_same_key_uses_cache_once(self):
        data = b'cached contents\n'
        entry = entry_for(data)
        gh = m.GitHub()
        api = Mock(return_value=blob_response(data))
        with patch.object(gh, 'api', api):
            self.assertEqual(gh.read(REPO, entry), data)
            self.assertEqual(gh.read(REPO, entry), data)
        api.assert_called_once_with(blob_path(REPO, entry['sha']))

    def test_same_sha_different_repo_not_shared(self):
        data = b'same digest, different repository\n'
        entry = entry_for(data)
        gh = m.GitHub()
        api = Mock(return_value=blob_response(data))
        with patch.object(gh, 'api', api):
            self.assertEqual(gh.read(REPO, entry), data)
            self.assertEqual(gh.read(OTHER_REPO, entry), data)
        self.assertEqual(api.call_count, 2)
        self.assertEqual({call.args[0] for call in api.call_args_list},
                         {blob_path(REPO, entry['sha']), blob_path(OTHER_REPO, entry['sha'])})

if __name__ == '__main__': unittest.main()
