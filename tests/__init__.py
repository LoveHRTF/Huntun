import os
import tempfile

# Tests never read or write the real ~/.huntun (saved model servers, the project list): a throwaway home for the run.
os.environ["HUNTUN_HOME"] = tempfile.mkdtemp(prefix="huntun-test-home-")
