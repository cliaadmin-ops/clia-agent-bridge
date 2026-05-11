import os
import time
import jwt
import requests
from git import Repo, GitCommandError

class GitOps:
    def __init__(self, repo_path, remote_url, app_id=None, private_key=None, installation_id=None, token=None):
        self.repo_path = repo_path
        self.base_remote_url = remote_url.replace("https://", "")
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.token = token
        self.token_expires = 0
        
        self.repo = self._get_repo()

    def _get_installation_token(self):
        """Generates a short-lived GitHub App Installation Access Token."""
        if not self.app_id or not self.private_key or not self.installation_id:
            return self.token # Fallback to static token if provided

        # Check if current token is still valid (with 5 min buffer)
        if self.token and time.time() < (self.token_expires - 300):
            return self.token

        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),
            "iss": self.app_id,
        }

        # Create JWT
        encoded_jwt = jwt.encode(payload, self.private_key, algorithm="RS256")

        # Exchange for Installation Token
        url = f"https://api.github.com/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {encoded_jwt}",
            "Accept": "application/vnd.github+json",
        }
        
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        self.token = data["token"]
        # Parse expiration (e.g., "2023-06-08T19:25:00Z")
        # For simplicity, we'll just set it to 1 hour from now
        self.token_expires = now + 3600
        
        return self.token

    def _get_authenticated_url(self):
        token = self._get_installation_token()
        return f"https://x-access-token:{token}@{self.base_remote_url}"

    def _get_repo(self):
        auth_url = self._get_authenticated_url()
        if not os.path.exists(self.repo_path):
            repo = Repo.clone_from(auth_url, self.repo_path)
        else:
            repo = Repo(self.repo_path)
            repo.remotes.origin.set_url(auth_url)
        
        # Configure Git identity
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "CLIA Agent")
            cw.set_value("user", "email", "agent@canadaragolake.com")
            
        return repo

    def sync_main(self):
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.checkout('main')
        self.repo.remotes.origin.pull()

    def create_content_branch(self, action_name):
        ts = int(time.time())
        branch_name = f"content-{action_name}-{ts}"
        new_branch = self.repo.create_head(branch_name)
        new_branch.checkout()
        return branch_name

    def commit_and_push(self, message):
        try:
            self.repo.remotes.origin.set_url(self._get_authenticated_url())
            self.repo.git.add(A=True)
            self.repo.index.commit(message)
            origin = self.repo.remote(name='origin')
            # Push the feature branch for history
            origin.push(self.repo.active_branch)
            # Force push to 'dev' branch to trigger the preview deployment
            self.repo.git.push('origin', f"{self.repo.active_branch.name}:dev", force=True)
            print(f"DEBUG: Git push successful: {message}")
        except Exception as e:
            print(f"DEBUG: Git push FAILED: {str(e)}")
            raise e

    def merge_to_main(self, branch_name):
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.checkout('main')
        self.repo.remotes.origin.pull()
        self.repo.git.merge(branch_name)
        self.repo.remotes.origin.push()
        # Optionally delete the branch
        self.repo.git.branch('-d', branch_name)
        self.repo.git.push('origin', '--delete', branch_name)

    def revert_main(self):
        """Reverts the last commit on the main branch."""
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.checkout('main')
        self.repo.remotes.origin.pull()
        # Revert the last commit
        self.repo.git.revert('HEAD', no_edit=True)
        self.repo.remotes.origin.push()
        return self.repo.head.commit.hexsha

    def get_dev_url(self):
        # The official Dev URL for the clia-dev service
        return "https://clia-dev-378290023292.us-east1.run.app"
