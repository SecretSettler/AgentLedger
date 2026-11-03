"""Exercise the optional hosted adapter when a recent Node runtime is installed."""
import shutil,subprocess,unittest
from pathlib import Path

@unittest.skipUnless(shutil.which("node"), "Node is optional; hosted adapter tests require it")
class HostedCommentTests(unittest.TestCase):
    def test_github_comment_adapter(self):
        version=subprocess.check_output(["node","-p","process.versions.node.split('.')[0]"],text=True).strip()
        if int(version)<18:
            self.skipTest("Node 18+ is required for fetch/Response")
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run(["node","--test",str(root/"tests/github_sync.mjs")],cwd=root,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)
