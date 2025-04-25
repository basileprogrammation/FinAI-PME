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

class Entreprise(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(100), unique=True, nullable=False)
    utilisateurs = db.relationship('User', backref='entreprise', lazy=True)  # relation vers les utilisateurs

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    date_creation = db.Column(db.DateTime, default=datetime.utcnow)
    alerts_enabled = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime)
    role = db.Column(db.String(20), default='user')  # "user" ou "admin"
    is_admin = db.Column(db.Boolean, default=False)
    entreprise_id = db.Column(db.Integer, db.ForeignKey('entreprise.id'))
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'))  # lien vers l’admin
    admin = db.relationship('User', remote_side=[id], backref='membres')  # liste des membres sous cet admin


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

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(100), nullable=False)
    montant_max = db.Column(db.Float, nullable=False)
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
    # Récupérer toutes les transactions de l’utilisateur
    txs = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.date).all()

    # Alertes IA existantes + budget
    alerts = generate_alerts(txs)

    return render_template('dashboard.html', user=current_user, alerts=alerts)


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

# ----------------------
# ROUTE: Inscription - /register
# ----------------------
# MODIFICATION DU CODE REGISTER POUR ATTRIBUER LE BON ADMIN PAR ENTREPRISE
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        entreprise_nom = request.form['entreprise']

        if User.query.filter_by(email=email).first():
            flash("Email déjà utilisé")
            return redirect('/register')

        # Créer ou retrouver l'entreprise
        entreprise = Entreprise.query.filter_by(nom=entreprise_nom).first()
        if not entreprise:
            entreprise = Entreprise(nom=entreprise_nom)
            db.session.add(entreprise)
            db.session.commit()

            # Créer automatiquement un admin pour cette nouvelle entreprise
            admin = User(
                email=f"admin_{entreprise_nom.lower()}@finai.com",
                password=generate_password_hash("admin123", method='pbkdf2:sha256', salt_length=8),
                is_admin=True,
                role="admin",
                entreprise_id=entreprise.id
            )
            db.session.add(admin)
            db.session.commit()
        else:
            admin = User.query.filter_by(entreprise_id=entreprise.id, is_admin=True).first()

        # Créer le nouvel utilisateur et l'associer à l'admin de l'entreprise
        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256', salt_length=8)
        new_user = User(
            email=email,
            password=hashed_pw,
            entreprise_id=entreprise.id,
            role='user',
            is_admin=False,
            admin_id=admin.id if admin else None
        )
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
# ------------------ BUDGETS ------------------
@app.route('/budgets', methods=['GET', 'POST'])
@login_required
def budgets():
    if request.method == 'POST':
        new_budget = Budget(
            category=request.form['category'],
            montant_max=request.form['montant_max'],
            user_id=current_user.id
        )
        db.session.add(new_budget)
        db.session.commit()
        flash("Budget ajouté avec succès.")
        return redirect('/budgets')
    
    budgets = Budget.query.filter_by(user_id=current_user.id).all()
    return render_template('budgets.html', budgets=budgets)

@app.route('/categories')
@login_required
def analyse_categories():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    txs = Transaction.query.filter_by(user_id=current_user.id).all()

    # Filtrage par date
    if start_date and end_date:
        start = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()
        txs = [t for t in txs if start <= t.date <= end]

    # Dépenses uniquement
    depenses = [t for t in txs if t.amount < 0]

    categorie_totaux = {}
    for t in depenses:
        if t.category in categorie_totaux:
            categorie_totaux[t.category] += abs(t.amount)
        else:
            categorie_totaux[t.category] = abs(t.amount)

    categorie_totaux = dict(sorted(categorie_totaux.items(), key=lambda x: x[1], reverse=True))

    return render_template('categories.html', data=categorie_totaux,
                           start_date=start_date, end_date=end_date)
from flask import make_response
import matplotlib.pyplot as plt
import base64
# ------------------ RAPPORTS ------------------
@app.route('/rapport')
@login_required
def rapport_pdf():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # Récupérer transactions de l'utilisateur
    txs = Transaction.query.filter_by(user_id=current_user.id).all()
    if start_date and end_date:
        start = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()
        txs = [t for t in txs if start <= t.date <= end]

    total_revenus = sum(t.amount for t in txs if t.amount > 0)
    total_depenses = sum(abs(t.amount) for t in txs if t.amount < 0)
    forecast_data = generate_forecast(txs)
    conseil = generer_conseil(txs, forecast_data["prevision"])
    alerts = generate_alerts(txs)

    # ✅ Générer une image de prévision
    plt.figure(figsize=(10, 4))
    historique = [t.amount for t in txs]
    labels = [t.date.strftime('%d/%m') for t in txs]
    plt.plot(labels, historique, label='Historique')
    plt.plot([p['day'] for p in forecast_data['prevision']],
             [p['value'] for p in forecast_data['prevision']], linestyle='--', label='Prévision')
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.legend()

    # Encode image to base64
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    image_base64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()

    html = render_template('rapport_template.html',
                           user=current_user,
                           total_revenus=total_revenus,
                           total_depenses=total_depenses,
                           forecast=forecast_data['prevision'],
                           alerts=alerts,
                           conseil=conseil,
                           start=start_date,
                           end=end_date,
                           chart=image_base64)

    output = BytesIO()
    pisa.CreatePDF(html, dest=output)
    output.seek(0)

    return send_file(output, as_attachment=True,
                     download_name="rapport_mensuel.pdf", mimetype="application/pdf")


# ----------------------
# ROUTE: Voir les utilisateurs de la même entreprise - /admin/utilisateurs
# ----------------------

@app.route('/admin/utilisateurs')
@login_required
def admin_utilisateurs():
    if not current_user.is_admin:
        flash("Accès réservé à l’administrateur.")
        return redirect('/home')

    search_email = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)
    per_page = 10

    query = User.query.filter_by(admin_id=current_user.id)
    if search_email:
        query = query.filter(User.email.ilike(f"%{search_email}%"))

    utilisateurs_pagines = query.paginate(page=page, per_page=per_page)

    data_utilisateurs = []
    for utilisateur in utilisateurs_pagines.items:
        txs = Transaction.query.filter_by(user_id=utilisateur.id).all()
        total_depenses = round(sum(t.amount for t in txs if t.amount < 0), 2)
        total_revenus = round(sum(t.amount for t in txs if t.amount > 0), 2)
        data_utilisateurs.append({
            "id": utilisateur.id,
            "email": utilisateur.email,
            "role": utilisateur.role,
            "date_creation": utilisateur.date_creation,
            "last_login": utilisateur.last_login,
            "nb_transactions": len(txs),
            "depenses": total_depenses,
            "revenus": total_revenus,
            "is_admin": utilisateur.is_admin
        })

    return render_template(
        'admin_utilisateurs.html',
        utilisateurs=data_utilisateurs,
        pagination=utilisateurs_pagines,
        search=search_email
    )


# ----------------------
# ROUTE: Voir transactions d’un utilisateur - /admin/utilisateur/<id>/transactions
# ----------------------

@app.route('/admin/utilisateur/<int:user_id>/transactions')
@login_required
def admin_utilisateur_transactions(user_id):
    if not current_user.is_admin:
        return redirect('/home')

    utilisateur = User.query.get_or_404(user_id)

    # Filtres
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    category = request.args.get('category')

    query = Transaction.query.filter_by(user_id=user_id)

    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if category:
        query = query.filter(Transaction.category.ilike(f"%{category}%"))

    transactions = query.order_by(Transaction.date.desc()).all()

    # Graphique : total par catégorie
    category_totals = {}
    for tx in transactions:
        category_totals[tx.category] = category_totals.get(tx.category, 0) + tx.amount

    return render_template('admin_transactions.html',
                           utilisateur=utilisateur,
                           transactions=transactions,
                           start_date=start_date,
                           end_date=end_date,
                           category=category,
                           category_totals=category_totals)


# ----------------------
# ROUTE: Export PDF des transactions d’un utilisateur - /admin/utilisateur/<id>/export/pdf
# ----------------------

@app.route('/admin/utilisateur/<int:user_id>/export/pdf')
@login_required
def export_pdf_admin(user_id):
    if current_user.role != 'admin':
        flash("Accès refusé.")
        return redirect('/home')

    utilisateur = User.query.get_or_404(user_id)
    if utilisateur.entreprise_id != current_user.entreprise_id:
        flash("Accès refusé.")
        return redirect('/home')

    txs = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.date).all()
    html = render_template('pdf_template.html', transactions=txs)
    output = BytesIO()
    pisa.CreatePDF(html, dest=output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"{utilisateur.email}_transactions.pdf")


# ----------------------
# ROUTE: Modifier une transaction (admin) - /admin/transaction/update
# ----------------------

@app.route('/admin/transaction/update', methods=['POST'])
@login_required
def admin_update_transaction():
    if not current_user.is_admin:
        return redirect('/home')

    tx_id = request.form['id']
    tx = Transaction.query.get_or_404(tx_id)

    tx.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
    tx.category = request.form['category']
    tx.amount = float(request.form['amount'])

    db.session.commit()
    flash("Transaction mise à jour avec succès")
    return redirect(f"/admin/utilisateur/{tx.user_id}/transactions")

@app.route('/admin/promouvoir/<int:user_id>')
@app.route('/admin/promouvoir/<int:user_id>')
@login_required
def promouvoir_admin(user_id):
    if not current_user.is_admin:
        return redirect('/home')

    user = User.query.get_or_404(user_id)
    user.is_admin = True
    user.role = "admin"  # ✅ Mettre à jour le rôle aussi
    db.session.commit()
    flash(f"{user.email} est maintenant administrateur.")
    return redirect('/admin/utilisateurs')


@app.route('/admin/destituer/<int:user_id>')
@login_required
def retirer_admin(user_id):
    if not current_user.is_admin:
        return redirect('/home')

    user = User.query.get_or_404(user_id)
    user.is_admin = False
    user.role = "user"  # ✅ Revenir au rôle "user"
    db.session.commit()
    flash(f"{user.email} n'est plus administrateur.")
    return redirect('/admin/utilisateurs')


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        return redirect('/home')

    page = request.args.get('page', 1, type=int)
    per_page = 10

    # ✅ uniquement les utilisateurs de l’admin connecté
    users_paginated = User.query.filter_by(admin_id=current_user.id).paginate(page=page, per_page=per_page)
    users = users_paginated.items
    user_ids = [u.id for u in users]

    transactions = Transaction.query.filter(Transaction.user_id.in_(user_ids)).all()
    total_transactions = len(transactions)
    total_depenses = sum(t.amount for t in transactions if t.amount < 0)
    total_revenus = sum(t.amount for t in transactions if t.amount > 0)

    return render_template('admin_dashboard.html',
                           users=users,
                           total_transactions=total_transactions,
                           total_depenses=total_depenses,
                           total_revenus=total_revenus,
                           pagination=users_paginated)


# ------------------ LES FONCTIONS UTILIES ---------------------------
# ------------------ FORECAST------------------
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
    
    # 🔔 Alerte si dépassement du budget
    # Alertes budget
    user_id = transactions[0].user_id if transactions else None
    if user_id:
        budgets = Budget.query.filter_by(user_id=user_id).all()
        for budget in budgets:
            total_cat = sum(t.amount for t in transactions if t.category == budget.category)
            if abs(total_cat) > budget.montant_max:
                alerts.append(f"🚨 Dépassement du budget pour {budget.category} : {abs(total_cat)} € > {budget.montant_max} €")

    return alerts
@app.template_filter('format_last_login')
def format_last_login(date):
    if not date:
        return "Jamais"

    now = datetime.utcnow()
    diff = now - date

    if diff.days == 0:
        return f"Aujourd’hui à {date.strftime('%Hh%M')}"
    elif diff.days == 1:
        return f"Hier à {date.strftime('%Hh%M')}"
    elif diff.days < 7:
        return f"{date.strftime('%A')} à {date.strftime('%Hh%M')}"  # ex : lundi à 14h30
    else:
        return date.strftime("Le %d/%m à %Hh%M")

# ------------------ MAIN ------------------

if __name__ == '__main__':
    with app.app_context():
        db.create_all()

        # Créer un compte admin si aucun admin n'existe
        if not User.query.filter_by(is_admin=True).first():
            from werkzeug.security import generate_password_hash
            admin = User(
                email="admin@finai.com",
                password=generate_password_hash("admin123", method='pbkdf2:sha256', salt_length=8),
                is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
            print("✅ Compte admin par défaut créé : admin@finai.com / admin123")
        # ✅ Mettre une date de connexion initiale à ceux qui en ont pas encore
        users = User.query.all()
        for u in users:
            if not u.last_login:
                u.last_login = datetime.utcnow()
        db.session.commit()
        print("✅ last_login mis à jour pour les utilisateurs sans date.")
    # ✅ Toujours exécuter le serveur ici, hors du if
    app.run(debug=True)
