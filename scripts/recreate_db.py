from services.model_loader import get_runtime_models
from utils.db_safety import assert_destructive_db_allowed

if __name__ == '__main__':
    app, db = get_runtime_models("app", "db")
    with app.app_context():
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="recreate_db.py::drop_all",
            testing=bool(app.config.get("TESTING")),
        )
        db.drop_all()
        db.create_all()
        print('Database recreated with clone status fields')
