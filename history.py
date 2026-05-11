import os
from google.cloud import firestore
from typing import List, Dict

# Initialize Async Firestore Client
# In Cloud Run, credentials are automatically picked up from the Service Account
db = firestore.AsyncClient()

class ChatHistoryManager:
    def __init__(self, collection_name: str = "chat_history"):
        self.collection = db.collection(collection_name)

    async def get_message_by_id(self, message_id: str):
        """
        Retrieves a message by its ID.
        """
        try:
            doc = await self.collection.document(message_id).get()
            if doc.exists:
                return doc.to_dict()
            return None
        except Exception as e:
            print(f"Error retrieving from Firestore: {e}")
            return None

    async def update_message(self, message_id: str, new_content: str):
        """
        Updates a message in Firestore by its ID.
        """
        try:
            doc_ref = self.collection.document(message_id)
            await doc_ref.update({"content": new_content})
        except Exception as e:
            print(f"Error updating Firestore: {e}")

    async def save_message(self, user_email: str, role: str, content: str):
        """
        Saves a message to Firestore and returns the document ID.
        """
        try:
            doc_data = {
                "user_email": user_email,
                "role": role,
                "content": content,
                "timestamp": firestore.SERVER_TIMESTAMP
            }
            # add() returns (timestamp, doc_ref)
            _, doc_ref = await self.collection.add(doc_data)
            return doc_ref.id
        except Exception as e:
            print(f"Error saving to Firestore: {e}")
            return None

    async def get_recent_messages(self, user_email: str, limit: int = 10) -> List[Dict]:
        """
        Retrieves the last N messages for a user, ordered chronologically.
        """
        try:
            # Simplified query to avoid composite index requirement
            query = (
                self.collection
                .where("user_email", "==", user_email)
                .limit(limit)
            )
            
            docs = await query.get()
            
            # Convert to list and sort in Python
            history = []
            for doc in docs:
                msg = doc.to_dict()
                history.append({
                    "role": msg["role"], 
                    "content": msg["content"],
                    "timestamp": msg.get("timestamp")
                })
            
            # Sort by timestamp (handling potential None values)
            history.sort(key=lambda x: x["timestamp"] if x["timestamp"] else 0)
            
            # Return only role and content
            return [{"role": h["role"], "content": h["content"]} for h in history]
        except Exception as e:
            print(f"Error retrieving from Firestore: {e}")
            return []

    async def clear_history(self, user_email: str):
        """Deletes all messages for a user."""
        try:
            query = self.collection.where("user_email", "==", user_email)
            docs = await query.get()
            for doc in docs:
                await doc.reference.delete()
            print(f"DEBUG: Cleared history for {user_email}")
        except Exception as e:
            print(f"Error clearing history: {e}")

    async def acquire_lock(self, user_email: str, timeout_mins: int = 10) -> bool:
        """
        Attempts to acquire the global agent lock in Firestore.
        """
        try:
            lock_ref = db.collection("system").document("agent_lock")
            
            # Use a transaction to ensure atomic lock acquisition
            @firestore.async_transactional
            async def _transactional_lock(transaction):
                snapshot = await lock_ref.get(transaction=transaction)
                now = time.time()
                
                if snapshot.exists:
                    data = snapshot.to_dict()
                    # Check if lock is held by someone else and not expired
                    if data.get("locked") and data.get("user") != user_email:
                        if now - data.get("timestamp", 0) < (timeout_mins * 60):
                            return False
                
                # Acquire or renew lock
                transaction.set(lock_ref, {
                    "locked": True,
                    "user": user_email,
                    "timestamp": now
                })
                return True

            return await _transactional_lock(db.transaction())
        except Exception as e:
            print(f"Error acquiring lock: {e}")
            return False

    async def release_lock(self, user_email: str):
        """
        Releases the global agent lock if held by the user.
        """
        try:
            lock_ref = db.collection("system").document("agent_lock")
            snapshot = await lock_ref.get()
            if snapshot.exists:
                data = snapshot.to_dict()
                if data.get("user") == user_email:
                    await lock_ref.update({"locked": False})
        except Exception as e:
            print(f"Error releasing lock: {e}")

    async def is_locked(self, user_email: str, timeout_mins: int = 10) -> Dict:
        """
        Checks if the agent is currently locked by another user.
        """
        try:
            lock_ref = db.collection("system").document("agent_lock")
            snapshot = await lock_ref.get()
            if snapshot.exists:
                data = snapshot.to_dict()
                now = time.time()
                if data.get("locked") and data.get("user") != user_email:
                    if now - data.get("timestamp", 0) < (timeout_mins * 60):
                        return {"locked": True, "user": data.get("user")}
            return {"locked": False}
        except Exception as e:
            print(f"Error checking lock: {e}")
            return {"locked": False}
