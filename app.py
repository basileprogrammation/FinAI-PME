import os
import pandas as pd
from flask import Flask, render_template, request, redirect, send_file, jsonify, flash, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from datetime import datetime
from io import BytesIO
from xhtml2pdf import pisa
from prophet import Prophet

load_dotenv()

app = Flask(__name__)
app.secret_key = 'secret-finai'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ------------------ MODELES ------------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    alerts_enabled = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime)  # 👈 nouvelle colonne

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date)
    category = db.Column(db.String(100))
    amount = db.Column(db.Float)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

class Alerte(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(300))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

# ------------------ LOGIN ------------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ------------------ ROUTES ------------------

@app.route('/')
def splash():
    return render_template('splash.html')

@app.route('/home')
@login_required
def home():
    return render_template('dashboard.html', user=current_user)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and check_password_hash(user.password, request.form['password']):
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user)
            return redirect('/home')
        else:
            flash("Identifiants incorrects")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if User.query.filter_by(email=request.form['email']).first():
            flash("Email déjà utilisé")
            return redirect('/register')
        hashed_pw = generate_password_hash(request.form['password'], method='pbkdf2:sha256', salt_length=8)
        new_user = User(email=request.form['email'], password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        flash("Inscription réussie !")
        return redirect('/login')
    return render_template('register.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect('/login')

# ------------------ TRANSACTIONS ------------------

@app.route('/transactions')
@login_required
def transactions():
    selected = request.args.get('categorie')
    periode = request.args.get('periode')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = Transaction.query.filter_by(user_id=current_user.id)

    if selected:
        query = query.filter_by(category=selected)

    # ✅ Période dynamique
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)

    # Optionnel : filtres fixes en plus
    if periode == 'mois':
        query = query.filter(Transaction.date >= date.today().replace(day=1))
    elif periode == 'semaine':
        start_of_week = date.today() - timedelta(days=date.today().weekday())
        query = query.filter(Transaction.date >= start_of_week)

    txs = query.order_by(Transaction.date).all()
    total = sum(t.amount for t in txs)
    alerts = generate_alerts(txs)
    categories = list({t.category for t in Transaction.query.filter_by(user_id=current_user.id).all()})
    return render_template(
        'transactions.html',
        transactions=txs,
        alerts=alerts,
        categories=categories,
        selected=selected,
        periode=periode,
        total=total,
        start_date=start_date,
        end_date=end_date
    )

@app.route('/add', methods=['POST'])
@login_required
def add():
    tx = Transaction(
        date=datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
        category=request.form['category'],
        amount=float(request.form['amount']),
        user_id=current_user.id
    )
    db.session.add(tx)
    db.session.commit()
    flash("Transaction ajoutée")
    return redirect('/transactions')

@app.route('/update', methods=['POST'])
@login_required
def update():
    tx = Transaction.query.get(request.form['id'])
    if tx and tx.user_id == current_user.id:
        tx.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        tx.category = request.form['category']
        tx.amount = request.form['amount']
        db.session.commit()
        flash("Transaction mise à jour")
    return redirect('/transactions')

@app.route('/delete', methods=['POST'])
@login_required
def delete():
    tx = Transaction.query.get(request.form['id'])
    if tx and tx.user_id == current_user.id:
        db.session.delete(tx)
        db.session.commit()
        flash("Transaction supprimée")
    return redirect('/transactions')

@app.route('/import', methods=['POST'])
@login_required
def import_csv():
    file = request.files['file']
    if file.filename.endswith('.csv'):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
        file.save(filepath)
        df = pd.read_csv(filepath)
        for _, row in df.iterrows():
            tx = Transaction(
                date=pd.to_datetime(row['date']).date(),
                category=row['category'],
                amount=row['amount'],
                user_id=current_user.id
            )
            db.session.add(tx)
        db.session.commit()
        flash("Transactions importées")
    return redirect('/transactions')

# ------------------ EXPORT ------------------

@app.route('/export/excel')
@login_required
def export_excel():
    txs = Transaction.query.filter_by(user_id=current_user.id).all()
    df = pd.DataFrame([(t.date, t.category, t.amount) for t in txs], columns=['Date', 'Catégorie', 'Montant'])
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="transactions.xlsx")

@app.route('/export/pdf')
@login_required
def export_pdf():
    txs = Transaction.query.filter_by(user_id=current_user.id).all()
    html = render_template('pdf_template.html', transactions=txs)
    output = BytesIO()
    pisa.CreatePDF(html, dest=output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="transactions.pdf")

# ------------------ PRÉVISION IA ------------------
def generer_conseil(transactions, prevision):
    if not transactions:
        return "Ajoutez quelques transactions pour que FinAI puisse vous conseiller intelligemment."

    total_revenus = sum(t.amount for t in transactions if t.amount > 0)
    total_depenses = sum(t.amount for t in transactions if t.amount < 0)
    total_depenses_abs = abs(total_depenses)

    prevision_negative = any(p['value'] < 0 for p in prevision)

    if prevision_negative:
        return "🚨 Votre trésorerie pourrait devenir négative bientôt. Réduisez les dépenses variables ou anticipez une rentrée d'argent."
    elif total_depenses_abs > total_revenus:
        return "⚠️ Vos dépenses sont plus élevées que vos revenus. Analysez vos dépenses fixes et optimisez les postes non essentiels."
    elif total_revenus > total_depenses_abs * 1.5:
        return "✅ Bonne gestion ! Vous pourriez placer votre excédent ou constituer une réserve de sécurité."
    elif total_revenus == 0:
        return "🟡 Aucun revenu détecté récemment. Enregistrez vos ventes ou pensez à diversifier vos sources de revenus."
    else:
        return "📊 Vos dépenses sont stables. Continuez à suivre vos transactions pour garder le cap."

@app.route('/forecast')
@login_required
def forecast():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = Transaction.query.filter_by(user_id=current_user.id)
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)

    txs = query.order_by(Transaction.date).all()
    forecast_data = generate_forecast(txs)

    total_revenus = sum(t.amount for t in txs if t.amount >= 0)
    total_depenses = abs(sum(t.amount for t in txs if t.amount < 0))
    solde_prevu = forecast_data["prevision"][-1]["value"] if forecast_data["prevision"] else 0

    if len(forecast_data["prevision"]) >= 2:
        debut = forecast_data["prevision"][0]["value"]
        fin = forecast_data["prevision"][-1]["value"]
        tendance = "hausse 📈" if fin > debut else "baisse 📉"
    else:
        tendance = "stable"

    score = solde_prevu + total_revenus - total_depenses
    if score >= 1000:
        sante = {"label": "Saine 🟢", "class": "bg-green-100 text-green-800"}
    elif score >= 0:
        sante = {"label": "Fragile 🟡", "class": "bg-yellow-100 text-yellow-800"}
    else:
        sante = {"label": "Critique 🔴", "class": "bg-red-100 text-red-800"}

    conseil = generer_conseil(txs, forecast_data["prevision"])

    return render_template('forecast.html',
        historique=forecast_data["historique"],
        prevision=forecast_data["prevision"],
        alerte=forecast_data["alerte"],
        total_revenus=round(total_revenus, 2),
        total_depenses=round(total_depenses, 2),
        solde_prevu=round(solde_prevu, 2),
        tendance=tendance,
        sante=sante,
        conseil=conseil,
        start_date=start_date,
        end_date=end_date
    )



def generate_forecast(transactions):
    if not transactions:
        return {'historique': [], 'prevision': [], 'alerte': False}

    # Historique : on inclut type 'revenu' ou 'depense'
    historique = [
        {
            "day": t.date.strftime('%d/%m'),
            "value": t.amount,
            "type": "revenu" if t.amount >= 0 else "depense"
        }
        for t in transactions
    ]

    # Prévision avec Prophet
    df = pd.DataFrame([(t.date, t.amount) for t in transactions], columns=["ds", "y"])
    model = Prophet()
    model.fit(df)
    future = model.make_future_dataframe(periods=30)
    forecast = model.predict(future)

    prevision = forecast[['ds', 'yhat']].tail(30)
    prevision_data = [{"day": row['ds'].strftime('%d/%m'), "value": round(row['yhat'], 2)} for _, row in prevision.iterrows()]
    alerte = any(row['yhat'] < 0 for _, row in prevision.iterrows())

    return {
        "historique": historique,
        "prevision": prevision_data,
        "alerte": alerte
    }

    
# ------------------ ALERTES IA ------------------

def generate_alerts(transactions):
    alerts = []
    total = sum(t.amount for t in transactions)
    if total < 0:
        alerts.append("⚠️ Solde total négatif : attention au découvert.")
    for t in transactions:
        if abs(t.amount) > 2000:
            alerts.append(f"💸 Dépense importante : {t.amount} € le {t.date}")
    return alerts

# ------------------ MAIN ------------------

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
