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
        """
        Creates a new branch from the current state of 'dev' to ensure
        incremental updates don't conflict with pending changes.
        """
        self.repo.remotes.origin.set_url(self._get_authenticated_url())
        self.repo.git.fetch('--all')
        self.repo.git.checkout('dev')
        self.repo.git.reset('--hard', 'origin/dev')
        self.repo.git.clean('-fd')
        
        ts = int(time.time())
        branch_name = f"content-{action_name}-{ts}"
        new_branch = self.repo.create_head(branch_name)
        new_branch.checkout()
        return branch_name

    def commit_and_push(self, message):
        """
        Commits changes to the feature branch and merges it into 'dev'.
        Returns True if changes were pushed, False otherwise.
        """
        is_dirty = self.repo.is_dirty(untracked_files=True)
        print(f"DEBUG: commit_and_push - is_dirty: {is_dirty}")
        if not is_dirty:
            print(f"DEBUG: No changes detected in {self.repo_path}. Status: {self.repo.git.status()}")
            return False

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
                return True
                
            self.repo.remotes.origin.push()
            self.repo.git.checkout(current_branch)
            return True
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

    def revert_dev(self):
        """
        Rolls back the last commit on 'dev' using an atomic remote push.
        Uses force-with-lease to prevent accidental overwrites if the remote moved.
        """
        try:
            self.repo.remotes.origin.set_url(self._get_authenticated_url())
            # Fetch latest to ensure origin/dev is current
            self.repo.remotes.origin.fetch('dev')
            
            # Atomic move: push the parent of origin/dev to dev
            # This effectively "undos" the last commit on the remote
            self.repo.git.push('origin', 'origin/dev~1:dev', force_with_lease=True)
            
            # Sync local state
            self.repo.git.checkout('dev')
            self.repo.git.reset('--hard', 'origin/dev')
            return True
        except Exception as e:
            print(f"DEBUG: Failed to revert dev: {e}")
            return False

    def revert_main(self):
        """
        Rolls back the last commit on BOTH 'main' and 'dev' using atomic remote pushes.
        This ensures that a production rollback also clears the staging branch.
        """
        try:
            self.repo.remotes.origin.set_url(self._get_authenticated_url())
            
            # 1. Revert Main
            self.repo.remotes.origin.fetch('main')
            self.repo.git.push('origin', 'origin/main~1:main', force_with_lease=True)
            self.repo.git.checkout('main')
            self.repo.git.reset('--hard', 'origin/main')
            new_sha = self.repo.git.rev_parse('HEAD')

            # 2. Revert Dev (to keep it in sync with the rolled-back main)
            self.repo.remotes.origin.fetch('dev')
            self.repo.git.push('origin', 'origin/dev~1:dev', force_with_lease=True)
            self.repo.git.checkout('dev')
            self.repo.git.reset('--hard', 'origin/dev')
            
            return new_sha
        except Exception as e:
            print(f"DEBUG: Failed to revert production: {e}")
            raise e

    def get_dev_url(self):
        return "https://clia-dev-378290023292.us-east1.run.app"
