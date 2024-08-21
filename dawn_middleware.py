import os
from flask import Flask, Blueprint, request, Response, json, jsonify
import requests
import time
import sqlite3

"""
This Flask application integrates with the Relevance API to provide a custom language model interface.
It includes routes for chat completions and creating transient assistants, along with database operations
for storing conversation information.
"""

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

custom_llm = Blueprint('custom_llm', __name__)

# Constants
REGION = os.environ.get('RELEVANCE_REGION')
BASE_URL = os.environ.get('RELEVANCE_API_BASE_URL').format(region=REGION)
RELEVANCE_PROJECT_ID = os.environ.get('RELEVANCE_PROJECT_ID')
RELEVANCE_API_KEY = os.environ.get('RELEVANCE_API_KEY')
MAX_POLL_ATTEMPTS = 120
POLL_DELAY = 1

# HELPER FUNCTIONS

def setup_database():
    """
    Sets up the SQLite database and creates the 'conversation' table if it doesn't exist.
    This function should be called before the application starts handling requests.
    """
    conn = sqlite3.connect('conversations.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS conversation
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, relevance_agent_id TEXT, relevance_conversation_id TEXT)''')
    conn.commit()
    conn.close()

def delete_all_records():
    """
    Deletes all records from the conversation table.
    """
    try:
        conn = sqlite3.connect('conversations.db')
        cursor = conn.cursor()
        cursor.execute("DELETE FROM conversation")
        conn.commit()
        print("All records deleted successfully.")
    except sqlite3.Error as e:
        print(f"An error occurred while deleting records: {e}")
    finally:
        conn.close()



def trigger_agent(relevance_agent_id, user_content):
    """
    Triggers a Relevance agent with the given user content.

    Args:
        relevance_agent_id (str): The ID of the Relevance agent to trigger.
        user_content (str): The user's message content.

    Returns:
        dict: The JSON response from the Relevance API, or None if an error occurred.
    """
    url = f"{BASE_URL}/agents/trigger"
    payload = {
        "message": {
            "role": "user",
            "content": user_content
        },
        "agent_id": relevance_agent_id
    }

    conn = sqlite3.connect('conversations.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM conversation")
    conversation = cursor.fetchone()
    conn.close()

    if conversation:       
        relevance_conversation_id = conversation[2]

    if relevance_conversation_id != '1234':
        payload["conversation_id"] = relevance_conversation_id

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"{RELEVANCE_PROJECT_ID}:{RELEVANCE_API_KEY}"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        json_response = response.json()
        print(f"Full API Response: {json_response}")
        return json_response
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")
        if hasattr(e.response, 'text'):
            print(f"Response content: {e.response.text}")
        return None

def poll_for_updates(studio_id, job_id):
    """
    Polls the Relevance API for updates on a specific job.

    Args:
        studio_id (str): The ID of the studio.
        job_id (str): The ID of the job to poll for.

    Returns:
        dict: The output of the job if successful, or None if polling failed or timed out.
    """
    url = f"{BASE_URL}/studios/{studio_id}/async_poll/{job_id}"
    headers = {
        'Authorization': f"{RELEVANCE_PROJECT_ID}:{RELEVANCE_API_KEY}"
    }
    
    for _ in range(MAX_POLL_ATTEMPTS):
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            status = response.json()            
            
            if status['type'] == 'complete':
                for update in status.get('updates', []):
                    if update['type'] == 'chain-success':
                        return update['output']['output']
            
            time.sleep(POLL_DELAY)
        except requests.exceptions.RequestException as e:
            print(f"An error occurred while polling: {e}")
            return None
    
    print("Max polling attempts reached without success")
    return None

def return_assistant_config():
    """
    Creates and returns a configuration for a custom language model assistant.

    Returns:
        dict: The assistant configuration.
    """
    assistant_config = {
        "assistant": {
            "transcriber": {
                "provider": "deepgram",
                "model": "nova-2",
                "language": "en"
            },
            "model": {
                "messages": [
                    {
                        "role": "system",
                        "content": "Answer the callers questions as succintly as possible. Yes or no answers are completely acceptable when appropriate."
                    }
                ],
                "provider": "custom-llm",
                "model": "a6fdacc8-cc99-4334-88a8-5d0d85e4be52",
                "url": "https://29e9-24-96-15-35.ngrok-free.app/",                   
                "maxTokens": 250
            },
            "voice": {
                "provider": "azure",
                "voiceId": "andrew",
                "speed": 1
            },
            "firstMessageMode": "assistant-speaks-first",
            "hipaaEnabled": False,
            "recordingEnabled": True,
            "firstMessage": "Hey. Hi. Howdy.",
            "voicemailDetection": {
                "provider": "twilio"
            }
        }
    }    
    return assistant_config

# ENDPOINTS

@custom_llm.route('/manage-vapi-server-messages', methods=['POST'])
def manage_vapi_server_messages():
    if request.method != 'POST':
        return jsonify({'error': 'Invalid request method'}), 405
     
    try:
        request_data = request.get_json()
    except Exception as e:
        return jsonify({'error': 'Invalid JSON'}), 400
    
    type = request_data.get('message', {}).get('type')
    if type == 'assistant-request':
        print(f"VAPI Server Message Status: {type}" )
        assistant_config = return_assistant_config()
        return jsonify(assistant_config)
    elif type == 'status-update':
        status = request_data.get('message', {}).get('status')
        if status == 'ended':
            print("Call ended. Deleting all records.")
            delete_all_records()
            return jsonify({'message': 'Call ended. All records deleted.'}), 200
        
    return jsonify({'message': f'{type} request processed successfully'}), 200  

@custom_llm.route('/chat/completions', methods=['POST'])
def chat_completions():
    request_data = request.get_json()

    relevance_agent_id = request_data.get('model')
    
    try:
        conn = sqlite3.connect('conversations.db')
        c = conn.cursor()
        
        # Check if a record exists
        c.execute("SELECT relevance_agent_id, relevance_conversation_id FROM conversation LIMIT 1")
        existing_record = c.fetchone()
        
        if existing_record:
            existing_agent_id, existing_conversation_id = existing_record
            if existing_conversation_id != "1234":
                c.execute("UPDATE conversation SET relevance_agent_id = ?",
                          (relevance_agent_id,))
                relevance_conversation_id = existing_conversation_id
            else:
                relevance_conversation_id = "1234"
        else:
            # No record exists, insert a new one with the placeholder
            relevance_conversation_id = "1234"
            c.execute("INSERT INTO conversation (relevance_agent_id, relevance_conversation_id) VALUES (?, ?)",
                      (relevance_agent_id, relevance_conversation_id))
        
        conn.commit()
    except sqlite3.Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    finally:
        conn.close()   

    user_content = next((m['content'] for m in reversed(request_data.get('messages', [])) if m['role'] == 'user'), None)

    if not user_content:
        return jsonify({"error": "No user message found"}), 400

    job = trigger_agent(relevance_agent_id, user_content)
    if not job:
        return jsonify({"error": "Failed to trigger agent"}), 500

    if 'conversation_id' not in job or 'job_info' not in job:
        return jsonify({"error": "Invalid response from trigger_agent"}), 500

    studio_id = job['job_info'].get('studio_id')
    job_id = job['job_info'].get('job_id')
    
    if not studio_id or not job_id:
        return jsonify({"error": "Missing studio_id or job_id in response"}), 500

    agent_response = poll_for_updates(studio_id, job_id)

    if not agent_response:
        return jsonify({"error": "Failed to get agent response after polling"}), 500

    latest_response = agent_response.get('answer', '')

    # After getting the response from Relevance API
    if job and 'conversation_id' in job:
        real_conversation_id = job['conversation_id']
        if real_conversation_id != "1234":
            try:
                conn = sqlite3.connect('conversations.db')
                c = conn.cursor()
                c.execute("UPDATE conversation SET relevance_conversation_id = ?", (real_conversation_id,))
                conn.commit()
            except sqlite3.Error as e:
                print(f"Error updating conversation ID: {e}")
            finally:
                conn.close()

    def generate():
        words = latest_response.split()
        for word in words:
            json_data = json.dumps({
                'choices': [
                    {
                        'delta': {
                            'content': word + ' ',
                            'role': 'assistant'
                        }
                    }
                ]
            })
            yield f"data: {json_data}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), content_type='text/event-stream')

app.register_blueprint(custom_llm)
setup_database()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)