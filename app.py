"""
app.py
------
Flask entrypoint. This file's only job is to create the app, register the
API blueprint, and serve the single frontend page - every calculation
lives in the other modules.
"""

from flask import Flask, render_template

from api_routes import api


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(api)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app


# cPanel/Passenger's generated passenger_wsgi.py imports `application` from
# this file - this module-level object is what actually gets served.
application = create_app()

if __name__ == "__main__":
    application.run(debug=True, host="127.0.0.1", port=5000)
