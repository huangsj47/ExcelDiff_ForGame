from services.model_loader import get_runtime_models

if __name__ == '__main__':
    app, db = get_runtime_models("app", "db")
    with app.app_context():
        db.drop_all()
        db.create_all()
        print('Database recreated with clone status fields')
