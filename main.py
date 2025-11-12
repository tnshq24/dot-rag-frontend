from dotenv import load_dotenv

load_dotenv()

import os
import asyncio
import traceback
import tempfile
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, send_file, Response
import requests

from utility import (
    authenticate_user,
    generate_user_id,
    extract_pdf_references,
    get_relevant_sources,
    get_highlighted_pdf_content,
)

# from frontend.utility import (authenticate_user, generate_user_id,
#                                                extract_refs_dict,
#                                                get_relevant_sources, get_highlighted_pdf_content, extract_refs_dict_v2)


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "your-secret-key-here")
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")

def _auth_headers():
    token = session.get("access_token")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@app.route("/")
def index():
    """Main page with chat interface"""
    return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
    """Handle user login"""
    try:
        data = request.get_json()
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400

        # Authenticate against backend to obtain JWT
        resp = requests.post(
            f"{BACKEND_BASE_URL}/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        )
        if resp.status_code != 200:
            return jsonify({"error": "Invalid credentials"}), 401
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            return jsonify({"error": "Failed to obtain access token"}), 502
        user_id = generate_user_id(email)
        session["user_id"] = user_id
        session["user_email"] = email
        session["logged_in"] = True
        session["access_token"] = token
        return jsonify({"success": True, "user_id": user_id, "email": email})
    except Exception as e:
        return jsonify({"error": f"Login error: {str(e)}"}), 500


@app.route("/logout", methods=["POST"])
def logout():
    """Handle user logout"""
    session.clear()
    return jsonify({"success": True})


@app.route("/check_auth")
def check_auth():
    """Check if user is authenticated"""
    if session.get("logged_in"):
        if "admin" in session.get("user_email"):
            isadmin = True
        else:
            isadmin = False
        return jsonify(
            {
                "authenticated": True,
                "user_id": session.get("user_id"),
                "email": session.get("user_email"),
                "isadmin": isadmin,
            }
        )
    return jsonify({"authenticated": False})


@app.route("/chat_history")
def chat_history():
    """Get chat history for authenticated user"""
    if not session.get("logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        resp = requests.get(f"{BACKEND_BASE_URL}/chat_history", headers=_auth_headers(), timeout=30)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/user_sessions")
def user_sessions():
    """Get all sessions for authenticated user"""
    if not session.get("logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        resp = requests.get(f"{BACKEND_BASE_URL}/user_sessions", headers=_auth_headers(), timeout=30)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/view_highlights", methods=["POST"])
def view_highlights():
    source = request.get_json()
    if not source:
        return jsonify({"error": "No data provided"}), 400
    # Basic validation
    for field in ["filename", "page_number", "content"]:
        if not source.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400
    try:
        resp = requests.post(
            f"{BACKEND_BASE_URL}/view_highlights",
            json=source,
            headers=_auth_headers(),
            timeout=120,
            stream=True,
        )
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("application/pdf"):
            download_name = source.get("filename", "document.pdf")
            return Response(
                resp.content,
                mimetype="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{download_name}"',
                    "X-Page-Number": resp.headers.get("X-Page-Number", "1"),
                },
            )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    """Handle chat requests"""
    try:
        data = request.get_json()
        question = data.get("question", "").strip()
        user_id = data.get("user_id", "").strip()
        conversation_id = data.get("conversation_id", "").strip()
        session_id = data.get("session_id", "").strip()
        file_names = data.get("file_names", [])  # New parameter for selected files

        if not question:
            return jsonify({"error": "Please provide a question"}), 400

        body = {
            "question": question,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "file_names": file_names,
        }
        resp = requests.post(f"{BACKEND_BASE_URL}/chat", json=body, headers=_auth_headers(), timeout=120)
        if resp.status_code != 200:
            return jsonify(resp.json()), resp.status_code
        response = resp.json()
        # Extract and map references for UI highlighting support
        references_text = response.get("references", "")
        result_v2 = extract_pdf_references(references_text)
        relevant_sources = get_relevant_sources(result=result_v2, response={"source_documents": response.get("source_documents", [])})
        return jsonify(
            {
                "answer": response.get("answer", ""),
                "question": question,
                "timestamp": response.get("timestamp", ""),
                "source_documents": relevant_sources,
            }
        )
    except Exception as e:
        return (
            jsonify({"error": f"Error processing request: {traceback.format_exc()}"}),
            500,
        )


@app.route("/upload_pdf", methods=["POST"])
def upload_pdf():
    if "pdfs" not in request.files:
        return jsonify({"error": "No PDF files provided."}), 400

    files = request.files.getlist("pdfs")
    if len(files) == 0 or len(files) > 1:
        return jsonify({"error": "You must upload only 1 files."}), 400

    # Forward the multipart form-data to backend
    try:
        files_payload = [("pdfs", (f.filename, f.stream, f.mimetype)) for f in files]
        form = {
            "field1": request.form.get("field1", ""),
            "field2": request.form.get("field2", ""),
            "field3": request.form.get("field3", ""),
        }
        headers = {"Authorization": _auth_headers().get("Authorization", "")}
        resp = requests.post(f"{BACKEND_BASE_URL}/upload_pdf", files=files_payload, data=form, headers=headers, timeout=300)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/view_pdf/<blob_name>")
def view_pdf(blob_name):
    """Serve PDF files with proper content type for viewing in browser"""
    try:
        headers = {"Authorization": _auth_headers().get("Authorization", "")}
        resp = requests.get(f"{BACKEND_BASE_URL}/view_pdf/{blob_name}", headers=headers, timeout=60)
        if resp.status_code != 200:
            return jsonify(resp.json()), resp.status_code
        return Response(
            resp.content,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{blob_name}"'},
        )

    except Exception as e:
        return jsonify({"error": f"Error viewing PDF: {str(e)}"}), 500


@app.route("/health")
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "frontend": True})


@app.route("/speech_token")
def speech_token():
    """Return an Azure Speech service token or subscription key (short-lived token recommended).

    Expects environment variables AZURE_SPEECH_KEY and AZURE_SPEECH_REGION to be set.
    """
    try:
        speech_key = os.environ.get("AZURE_SPEECH_KEY")
        speech_region = os.environ.get("AZURE_SPEECH_REGION")

        if not speech_key or not speech_region:
            return jsonify({"error": "Speech key/region not configured on server"}), 500

        # Acquire a token from Azure Cognitive Services token endpoint
        # Docs: https://learn.microsoft.com/azure/cognitive-services/speech-service/rest-speech-to-text
        token_url = (
            f"https://{speech_region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
        )
        headers = {"Ocp-Apim-Subscription-Key": speech_key, "Content-Length": "0"}
        resp = requests.post(token_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return (
                jsonify(
                    {"error": "Failed to acquire speech token", "detail": resp.text}
                ),
                502,
            )

        access_token = resp.text
        return jsonify({"token": access_token, "region": speech_region})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/session_messages")
def session_messages():
    """Get all messages for a given session_id (for authenticated user)"""
    if not session.get("logged_in"):
        return jsonify({"error": "Not authenticated"}), 401
    user_id = session.get("user_id")
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    try:
        items = rag_pipeline.get_cosmo_user_sessions_message(
            user_id=user_id, session_id=session_id
        )
        return jsonify({"messages": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/delete_session", methods=["POST"])
def delete_session():
    """Delete all messages for a given session_id (for authenticated user)"""
    if not session.get("logged_in"):
        return jsonify({"error": "Not authenticated"}), 401
    user_id = session.get("user_id")
    data = request.get_json()
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    try:
        # Get all messages for this session
        status = rag_pipeline.delete_cosmo_chat_message(
            user_id=user_id, session_id=session_id
        )
        if status:
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Error deleting chat message"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/available_files")
def available_files():
    """Get all available files for authenticated user"""
    if not session.get("logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    try:
        # Run the async get_available_files function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            files = loop.run_until_complete(rag_pipeline.get_available_files())
            # print(f"Available files from backend: {files}")
            return jsonify({"files": files})
        finally:
            loop.close()
    except Exception as e:
        # print(f"Error in available_files endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Run the Flask app
    app.run(debug=True, host="0.0.0.0", port=5000)
