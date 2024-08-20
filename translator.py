"""
Flask Middleware for vapi.ai and relevance.ai integration

This Flask application serves as middleware between vapi.ai (handling telephony) 
and relevance.ai (managing logic and prompting for LLM interactions). It processes
requests from vapi.ai, interacts with the relevance.ai API, and streams responses back.

The main route '/chat/completions' handles incoming requests, triggers the appropriate
agent, and returns streamed responses.
"""

import os
from flask import Flask, Blueprint, request, Response, json, session, jsonify
import requests
import time

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

custom_llm = Blueprint('custom_llm', __name__)

# Constants
REGION = os.environ.get('RELEVANCE_REGION')
BASE_URL = os.environ.get('RELEVANCE_API_BASE_URL').format(region=REGION)
PROJECT_ID = os.environ.get('RELEVANCE_PROJECT_ID')
API_KEY = os.environ.get('RELEVANCE_API_KEY')
MAX_POLL_ATTEMPTS = 120
POLL_DELAY = 1

def trigger_agent(agent_id, user_content, conversation_id=None):
    """
    Trigger an agent in the relevance.ai platform.

    Args:
        agent_id (str): The ID of the agent to trigger.
        user_content (str): The user's message content.
        conversation_id (str, optional): The ID of an existing conversation.

    Returns:
        dict: The JSON response from the relevance.ai API, or None if an error occurs.
    """
    url = f"{BASE_URL}/agents/trigger"
    payload = {
        "message": {
            "role": "user",
            "content": user_content
        },
        "agent_id": agent_id
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"{PROJECT_ID}:{API_KEY}"
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
    Poll the relevance.ai API for updates on a specific job.

    Args:
        studio_id (str): The ID of the studio.
        job_id (str): The ID of the job to poll for.

    Returns:
        dict: The output of the completed job, or None if polling fails or times out.
    """
    url = f"{BASE_URL}/studios/{studio_id}/async_poll/{job_id}"
    headers = {
        'Authorization': f"{PROJECT_ID}:{API_KEY}"
    }
    
    for _ in range(MAX_POLL_ATTEMPTS):
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            status = response.json()
            
            print(f"Polling status: {status}")
            
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

@custom_llm.route('/chat/completions', methods=['POST'])
def custom_llm_route():
    """
    Handle incoming requests for chat completions.

    This route processes incoming requests from vapi.ai, interacts with the relevance.ai API,
    and returns streamed responses.

    Returns:
        Response: A Flask response object with streamed content.
    """
    request_data = request.get_json()
    
    # Set the agent_id in the session
    session['agent_id'] = request_data.get('model')

    agent_id = session.get('agent_id')
    conversation_id = session.get('conversation_id')

    user_content = next((m['content'] for m in reversed(request_data.get('messages', [])) if m['role'] == 'user'), None)

    if not user_content:
        return jsonify({"error": "No user message found"}), 400

    job = trigger_agent(agent_id, user_content, conversation_id)
    if not job:
        return jsonify({"error": "Failed to trigger agent"}), 500

    print(f"Job returned from trigger_agent: {job}")

    if 'conversation_id' not in job or 'job_info' not in job:
        return jsonify({"error": "Invalid response from trigger_agent"}), 500

    conversation_id = job['conversation_id']
    session['conversation_id'] = conversation_id

    studio_id = job['job_info'].get('studio_id')
    job_id = job['job_info'].get('job_id')
    
    if not studio_id or not job_id:
        return jsonify({"error": "Missing studio_id or job_id in response"}), 500

    agent_response = poll_for_updates(studio_id, job_id)

    if not agent_response:
        return jsonify({"error": "Failed to get agent response after polling"}), 500

    latest_response = agent_response.get('answer', '')

    def generate():
        """
        Generator function to stream the response word by word.

        Yields:
            str: JSON-formatted string containing each word of the response.
        """
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)