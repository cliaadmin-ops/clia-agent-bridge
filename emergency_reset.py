import asyncio
from history import ChatHistoryManager
from google.cloud import firestore

async def clear_amy_lock():
    manager = ChatHistoryManager()
    user_email = "amy@canadaragolake.com"
    
    print(f"Checking lock status for {user_email}...")
    lock_status = await manager.is_locked(user_email)
    print(f"Current Lock Status: {lock_status}")
    
    if lock_status.get("locked"):
        print(f"Releasing lock held by {lock_status.get('user')}...")
        await manager.release_lock(lock_status.get('user'))
        print("Lock released.")
    else:
        # Force release anyway just in case
        db = firestore.AsyncClient()
        lock_ref = db.collection("system").document("agent_lock")
        await lock_ref.update({"locked": False})
        print("Global lock force-released.")

    print(f"Clearing chat history for {user_email} to reset UI...")
    await manager.clear_history(user_email)
    print("History cleared.")

if __name__ == "__main__":
    asyncio.run(clear_amy_lock())
