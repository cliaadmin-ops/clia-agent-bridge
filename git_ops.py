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
        if not self.app_id or not self.private_key or not self.installation_id:
            return self.token
        if self.token and time.time() < (self.token_expires - 300):
            return self.token
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": self.app_id}
        encoded_jwt = jwt.encode(payload, self.private_key, algorithm="RS256")
        url = f"https://api.github.com/app/installations/{self.installation_id}/access_tokens"
        headers = {"Authorization": f"Bearer {encoded_jwt}", "Accept": "application/vnd.github+json"}
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        self.token = data["token"]
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
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "CLIA Agent")
            cw.set_value("user", "email", "agent@canadaragolake.com")
        return repo

    def sync_main(self):
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.fetch('--all')
        self.repo.git.checkout('main')
        self.repo.git.reset('--hard', 'origin/main')
        self.repo.git.clean('-fd')

    def create_content_branch(self, action_name):
        self.sync_main()
        ts = int(time.time())
        branch_name = f"content-{action_name}-{ts}"
        new_branch = self.repo.create_head(branch_name)
        new_branch.checkout()
        return branch_name

    def commit_and_push(self, message):
        """
        Commits changes to the feature branch and merges it into 'dev'.
        """
        if not self.repo.is_dirty(untracked_files=True):
            print("DEBUG: No changes detected. Skipping commit.")
            return

        try:
            self.repo.remotes.origin.set_url(self._get_authenticated_url())
            
            # 1. Commit changes on the feature branch
            self.repo.git.add(A=True)
            self.repo.index.commit(message)
            origin = self.repo.remote(name='origin')
            current_branch = self.repo.active_branch.name
            
            # 2. Push feature branch
            origin.push(current_branch)
            
            # 3. Update 'dev' branch with Clean Slate
            self.repo.git.fetch('--all')
            self.repo.git.checkout('dev')
            self.repo.git.reset('--hard', 'origin/dev')
            self.repo.git.clean('-fd')
            
            print(f"DEBUG: Merging {current_branch} into 'dev'")
            try:
                # Use 'theirs' to prioritize the agent's new content
                self.repo.git.merge(current_branch, X='theirs')
            except GitCommandError:
                # Fallback: Force dev to match the feature branch exactly
                self.repo.git.push('origin', f"{current_branch}:dev", force=True)
                return
                
            self.repo.remotes.origin.push()
            self.repo.git.checkout(current_branch)
        except Exception as e:
            print(f"DEBUG: Git push FAILED: {str(e)}")
            raise e

    def merge_to_main(self, branch_name):
        """
        Promotes 'dev' to 'main'.
        """
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.fetch('--all')
        
        # Ensure main is clean
        self.repo.git.checkout('main')
        self.repo.git.reset('--hard', 'origin/main')
        self.repo.git.clean('-fd')
        
        try:
            print("DEBUG: Promoting 'dev' branch to 'main'")
            # Merge origin/dev into main
            self.repo.git.merge('origin/dev', X='theirs')
        except GitCommandError:
            # If merge fails, force main to match dev
            self.repo.git.push('origin', 'dev:main', force=True)
            return
            
        self.repo.remotes.origin.push()
        try:
            self.repo.git.push('origin', '--delete', branch_name)
        except:
            pass

    def discard_dev_changes(self):
        """
        Resets 'dev' to match 'main' exactly.
        """
        try:
            self.repo.remotes.origin.set_url(self._get_authenticated_url())
            self.repo.remotes.origin.fetch()
            self.repo.git.checkout('dev')
            self.repo.git.reset('--hard', 'origin/main')
            self.repo.git.push('origin', 'dev', force=True)
            return True
        except Exception as e:
            print(f"DEBUG: Failed to discard dev changes: {e}")
            return False

    def get_dev_url(self):
        return "https://clia-dev-378290023292.us-east1.run.app"
