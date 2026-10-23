import os, re, csv, io, sqlite3, uuid, smtplib
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from functools import wraps
from flask import Flask, g, request, redirect, url_for, session, flash, abort, render_template, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from markupsafe import Markup

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DB_PATH=os.environ.get('SCCRS_DB', os.path.join(BASE_DIR,'sccrs.db'))
UPLOAD_DIR=os.path.join(BASE_DIR,'uploads'); os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED={'pdf','png','jpg','jpeg'}; MAX_UPLOAD=5*1024*1024
DEPARTMENTS=['Facilities','Hostel','Academics','IT Maintenance']
STATUSES=['Pending','In Progress','Resolved','Closed']
app=Flask(__name__); app.config['SECRET_KEY']=os.environ.get('SECRET_KEY','change-this-secret-before-deployment'); app.config['MAX_CONTENT_LENGTH']=MAX_UPLOAD

def now(): return datetime.now().isoformat(timespec='seconds')
def connect():
    if 'db' not in g:
        g.db=sqlite3.connect(DB_PATH); g.db.row_factory=sqlite3.Row; g.db.execute('PRAGMA foreign_keys=ON')
    return g.db
@app.teardown_appcontext
def close_db(exc):
    db=g.pop('db',None)
    if db: db.close()
def init_db():
    db=sqlite3.connect(DB_PATH); db.execute('PRAGMA foreign_keys=ON')
    # Migrate Increment 1 users table so officer and department_head roles are supported.
    info=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if info and "'officer'" not in (info[0] or ''):
        db.executescript('''PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON; ALTER TABLE users RENAME TO users_old;
        CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT UNIQUE, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('student','officer','department_head','admin')), department_id INTEGER REFERENCES departments(id), created_at TEXT NOT NULL);
        INSERT INTO users(id,student_id,name,email,password_hash,role,department_id,created_at) SELECT id,student_id,name,email,password_hash,role,NULL,created_at FROM users_old;
        DROP TABLE users_old; PRAGMA foreign_keys=ON;''')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,student_id TEXT UNIQUE,name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('student','officer','department_head','admin')),department_id INTEGER REFERENCES departments(id),created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id TEXT NOT NULL UNIQUE,student_id INTEGER NOT NULL REFERENCES users(id),department_id INTEGER NOT NULL REFERENCES departments(id),description TEXT NOT NULL,attachment_path TEXT,status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending','In Progress','Resolved','Closed')),assigned_officer_id INTEGER REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT '',priority TEXT NOT NULL DEFAULT 'Normal' CHECK(priority IN ('Normal','High')));
    CREATE TABLE IF NOT EXISTS resolution_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,complaint_id INTEGER NOT NULL REFERENCES complaints(id),user_id INTEGER NOT NULL REFERENCES users(id),note TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY AUTOINCREMENT,complaint_id INTEGER NOT NULL REFERENCES complaints(id),user_id INTEGER NOT NULL REFERENCES users(id),old_status TEXT,new_status TEXT NOT NULL,remarks TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id),complaint_id INTEGER NOT NULL REFERENCES complaints(id),message TEXT NOT NULL,created_at TEXT NOT NULL,read INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER REFERENCES users(id),complaint_id INTEGER REFERENCES complaints(id),action TEXT NOT NULL,details TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    ''')
    # Add columns when upgrading an older Increment-1 database.
    cols={r[1] for r in db.execute('PRAGMA table_info(complaints)')}
    for name,ddl in [
        ('assigned_officer_id','INTEGER REFERENCES users(id)'),
        ('updated_at',"TEXT NOT NULL DEFAULT ''"),
        ('priority',"TEXT NOT NULL DEFAULT 'Normal'"),
        ('due_at',"TEXT NOT NULL DEFAULT ''"),
        ('escalated_at',"TEXT NOT NULL DEFAULT ''"),
        ('escalation_reason',"TEXT NOT NULL DEFAULT ''")
    ]:
        if name not in cols: db.execute(f'ALTER TABLE complaints ADD COLUMN {name} {ddl}')
    for d in DEPARTMENTS: db.execute('INSERT OR IGNORE INTO departments(name) VALUES(?)',(d,))
    # Backfill deadlines for complaints created before SLA support was added.
    for row in db.execute("SELECT id,priority,created_at FROM complaints WHERE due_at='' OR due_at IS NULL").fetchall():
        try: due = (datetime.fromisoformat(row[2]) + timedelta(hours=24 if row[1]=='High' else 72)).isoformat(timespec='seconds')
        except (ValueError, TypeError): due = (datetime.now() + timedelta(hours=24 if row[1]=='High' else 72)).isoformat(timespec='seconds')
        db.execute('UPDATE complaints SET due_at=? WHERE id=?',(due,row[0]))
    if not db.execute('SELECT id FROM users WHERE email=?',('admin@campus.edu',)).fetchone():
        db.execute('INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)',('System Administrator','admin@campus.edu',generate_password_hash('Admin@123'),'admin',now()))
    db.commit(); db.close()

def current_user():
    uid=session.get('user_id')
    return connect().execute('SELECT u.*,d.name department FROM users u LEFT JOIN departments d ON d.id=u.department_id WHERE u.id=?',(uid,)).fetchone() if uid else None
@app.context_processor
def inject(): return {'me':current_user(),'statuses':STATUSES,'now_iso':now()}
def require(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            u=current_user()
            if not u: flash('Please log in first.','error'); return redirect(url_for('login'))
            if roles and u['role'] not in roles: abort(403)
            return fn(*a,**kw)
        return wrapped
    return deco
def audit(action, complaint_id=None, details=''):
    connect().execute('INSERT INTO audit_log(user_id,complaint_id,action,details,created_at) VALUES(?,?,?,?,?)',(current_user()['id'] if current_user() else None,complaint_id,action,details,now()))
def notify(user_id, complaint_id, message):
    if user_id: connect().execute('INSERT INTO notifications(user_id,complaint_id,message,created_at) VALUES(?,?,?,?)',(user_id,complaint_id,message,now()))
def send_email(to, subject, body):
    # Configure SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM to enable actual delivery.
    host=os.getenv('SMTP_HOST'); port=int(os.getenv('SMTP_PORT','587'))
    if not host: return False
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=os.getenv('SMTP_FROM',os.getenv('SMTP_USER','noreply@campus.edu')); msg['To']=to; msg.set_content(body)
    with smtplib.SMTP(host,port,timeout=10) as s:
        s.starttls()
        if os.getenv('SMTP_USER'): s.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASSWORD',''))
        s.send_message(msg)
    return True
def notify_and_email(user_id, cid, message, subject):
    db=connect(); notify(user_id,cid,message); user=db.execute('SELECT email FROM users WHERE id=?',(user_id,)).fetchone()
    if user:
        try: send_email(user['email'],subject,message)
        except Exception: app.logger.exception('Email notification failed')
def deadline_for(priority, created=None):
    """SLA: High priority is due in 24 hours; Normal priority in 72 hours."""
    start = datetime.fromisoformat(created) if created else datetime.now()
    return (start + timedelta(hours=24 if priority == 'High' else 72)).isoformat(timespec='seconds')

def least_loaded_officer(department_id):
    """Choose a same-department officer with the fewest active complaints."""
    return connect().execute("""
        SELECT u.id, u.name, COUNT(c.id) AS active_workload
        FROM users u
        LEFT JOIN complaints c ON c.assigned_officer_id=u.id AND c.status IN ('Pending','In Progress')
        WHERE u.role='officer' AND u.department_id=?
        GROUP BY u.id, u.name
        ORDER BY active_workload ASC, u.id ASC
        LIMIT 1
    """, (department_id,)).fetchone()

def ticket(): return 'TCK-'+date.today().strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:6].upper()
BASE='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCCRS</title><link rel="stylesheet" href="{{url_for('static',filename='style.css')}}"></head><body><header><a class="brand" href="{{url_for('dashboard')}}">SCCRS</a><span>Student Campus Complaint Resolution System</span><nav>{% if me %}<a href="{{url_for('dashboard')}}">Dashboard</a><a href="{{url_for('notifications')}}">Notifications</a><a href="{{url_for('logout')}}">Logout</a>{% endif %}</nav></header><main>{% with msgs=get_flashed_messages(with_categories=true) %}{% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}{% endwith %}{{body|safe}}</main><footer>Student Campus Complaint Resolution System · Increment 2 &amp; 3</footer></body></html>'''
def page(body, **ctx): return render_template_string(BASE,body=render_template_string(body,**ctx))
from flask import render_template_string
@app.route('/')
def index(): return redirect(url_for('dashboard') if current_user() else url_for('login'))
@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form.get('name','').strip(); sid=request.form.get('student_id','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password','')
        if not name or not sid or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',email) or len(pw)<8: flash('Enter all details, a valid email, and a password of at least 8 characters.','error')
        else:
            try: connect().execute('INSERT INTO users(student_id,name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)',(sid,name,email,generate_password_hash(pw),'student',now())); connect().commit(); flash('Registration successful. Please log in.','success'); return redirect(url_for('login'))
            except sqlite3.IntegrityError: flash('Email or Student ID already exists.','error')
    return page('''<section class="panel narrow"><h1>Create student account</h1><form method="post"><label>Full name<input name="name" required></label><label>Student ID<input name="student_id" required></label><label>Email<input type="email" name="email" required></label><label>Password (minimum 8 characters)<input type="password" name="password" minlength="8" required></label><button>Register</button></form><p>Already registered? <a href="{{url_for('login')}}">Log in</a></p></section>''')
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        u=connect().execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').strip().lower(),)).fetchone()
        if u and check_password_hash(u['password_hash'],request.form.get('password','')): session.clear(); session['user_id']=u['id']; return redirect(url_for('dashboard'))
        flash('Invalid email or password.','error')
    return page('''<section class="panel narrow"><h1>Sign in</h1><form method="post"><label>Email<input type="email" name="email" required></label><label>Password<input type="password" name="password" required></label><button>Login</button></form><p>New student? <a href="{{url_for('register')}}">Create account</a></p><p class="muted">Demo admin: admin@campus.edu / Admin@123</p></section>''')
@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))
@app.route('/dashboard')
@require()
def dashboard():
    u=current_user(); db=connect()
    if u['role']=='student': rows=db.execute('SELECT c.*,d.name department,o.name officer FROM complaints c JOIN departments d ON d.id=c.department_id LEFT JOIN users o ON o.id=c.assigned_officer_id WHERE c.student_id=? ORDER BY c.created_at DESC',(u['id'],)).fetchall(); return page('''<div class="heading"><div><h1>My complaints</h1><p>File a campus complaint and follow its progress.</p></div><a class="button" href="{{url_for('new_complaint')}}">+ New complaint</a></div>{{table(rows,student=true)}}''',rows=rows,table=table)
    if u['role']=='officer': rows=db.execute("SELECT c.*,d.name department,s.name student_name,o.name officer FROM complaints c JOIN departments d ON d.id=c.department_id JOIN users s ON s.id=c.student_id LEFT JOIN users o ON o.id=c.assigned_officer_id WHERE c.assigned_officer_id=? ORDER BY CASE WHEN c.due_at!='' AND c.due_at < ? AND c.status NOT IN ('Resolved','Closed') THEN 0 ELSE 1 END,c.due_at,c.created_at",(u['id'],now())).fetchall(); return page('''<h1>Officer workspace</h1><p>Complaints assigned to you. SLA: High = 24 hours; Normal = 72 hours. Overdue complaints appear first.</p>{{table(rows,actions=true)}}''',rows=rows,table=table)
    if u['role']=='department_head': rows=db.execute('SELECT c.*,d.name department,u.name student_name,o.name officer FROM complaints c JOIN departments d ON d.id=c.department_id JOIN users u ON u.id=c.student_id LEFT JOIN users o ON o.id=c.assigned_officer_id WHERE c.department_id=? ORDER BY c.created_at DESC',(u['department_id'],)).fetchall(); return page('''<h1>Department dashboard</h1><p>Review your department's queue and assign officers.</p>{{table(rows,actions=true,assign=true)}}''',rows=rows,table=table)
    rows=db.execute('SELECT c.*,d.name department,u.name student_name,o.name officer FROM complaints c JOIN departments d ON d.id=c.department_id JOIN users u ON u.id=c.student_id LEFT JOIN users o ON o.id=c.assigned_officer_id ORDER BY c.created_at DESC').fetchall()
    counts={s:db.execute('SELECT COUNT(*) FROM complaints WHERE status=?',(s,)).fetchone()[0] for s in STATUSES}
    return page('''<div class="heading"><div><h1>Admin dashboard</h1><p>Campus-wide complaint operations and reporting.</p></div><div class="actions"><a class="button" href="{{url_for('create_staff')}}">+ Create staff account</a><a class="button secondary" href="{{url_for('reports')}}">Reports</a></div></div><div class="stats">{% for s,n in counts.items() %}<div class="stat"><span>{{s}}</span><strong>{{n}}</strong></div>{% endfor %}<div class="stat"><span>Total complaints</span><strong>{{rows|length}}</strong></div></div><h2>All complaints</h2>{{table(rows,actions=true,assign=true)}}''',rows=rows,counts=counts,table=table)
def table(rows,student=False,actions=False,assign=False):
    # Reusable table markup. Action buttons appear only for authorized roles.
    return Markup(render_template_string('''{% if rows %}<div class="tablewrap"><table><thead><tr><th>Ticket</th>{% if not student %}<th>Student</th>{% endif %}<th>Department</th><th>Description</th><th>Status</th><th>Priority</th><th>Assigned officer</th><th>Deadline</th><th>Created</th><th>Action</th></tr></thead><tbody>{% for c in rows %}<tr><td><a href="{{url_for('complaint_detail',ticket_id=c['ticket_id'])}}">{{c['ticket_id']}}</a></td>{% if not student %}<td>{{c['student_name']}}</td>{% endif %}<td>{{c['department']}}</td><td>{{c['description']|truncate(90)}}</td><td><span class="status {{c['status']|lower|replace(' ','-')}}">{{c['status']}}</span></td><td>{{c['priority']}}</td><td>{{c['officer'] if 'officer' in c.keys() and c['officer'] else 'Unassigned'}}</td><td>{% if 'due_at' in c.keys() and c['due_at'] %}{{c['due_at'][:16].replace('T',' ')}}{% if c['status'] not in ['Resolved','Closed'] and c['due_at'] < now_iso %} <b class="overdue">OVERDUE</b>{% endif %}{% else %}—{% endif %}</td><td>{{c['created_at'][:10]}}</td><td><a href="{{url_for('complaint_detail',ticket_id=c['ticket_id'])}}">Open</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="empty">No complaints to display yet.</div>{% endif %}''',rows=rows))
@app.route('/complaints/new',methods=['GET','POST'])
@require('student')
def new_complaint():
    db=connect(); deps=db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    if request.method=='POST':
        did=request.form.get('department_id'); desc=request.form.get('description','').strip(); priority=request.form.get('priority','Normal'); f=request.files.get('attachment'); path=None
        if not did or not db.execute('SELECT id FROM departments WHERE id=?',(did,)).fetchone() or len(desc)<10: flash('Select a department and enter at least 10 characters.','error')
        elif priority not in ('Normal','High'): flash('Invalid priority.','error')
        elif f and f.filename and ('.' not in f.filename or f.filename.rsplit('.',1)[1].lower() not in ALLOWED): flash('Only PDF, PNG, JPG files are allowed.','error')
        else:
            if f and f.filename: path=uuid.uuid4().hex+'_'+secure_filename(f.filename); f.save(os.path.join(UPLOAD_DIR,path))
            tid=ticket(); created=now(); due=deadline_for(priority,created); officer=least_loaded_officer(int(did)); officer_id=officer['id'] if officer else None
            db.execute('INSERT INTO complaints(ticket_id,student_id,department_id,description,attachment_path,status,assigned_officer_id,created_at,updated_at,priority,due_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(tid,current_user()['id'],int(did),desc,path,'Pending',officer_id,created,created,priority,due))
            cid=db.execute('SELECT id FROM complaints WHERE ticket_id=?',(tid,)).fetchone()['id']
            db.execute('INSERT INTO status_history(complaint_id,user_id,old_status,new_status,remarks,created_at) VALUES(?,?,?,?,?,?)',(cid,current_user()['id'],None,'Pending','Complaint submitted',created))
            if officer_id:
                notify_and_email(officer_id,cid,'Complaint '+tid+' was auto-assigned to you because you have the lowest active workload in the selected department. Deadline: '+due,'New complaint assigned: '+tid)
                audit('Complaint auto-assigned',cid,'Officer ID '+str(officer_id)+'; lowest active workload in selected department')
            else:
                heads=db.execute("SELECT id FROM users WHERE role='admin' OR (role='department_head' AND department_id=?)",(int(did),)).fetchall()
                for head in heads: notify(head['id'],cid,'No resolution officer is configured for this department. Please create an officer or assign this complaint: '+tid)
                audit('Complaint awaiting assignment',cid,'No officer exists in the selected department')
            audit('Complaint created',cid,tid); db.commit()
            flash(('Complaint submitted and automatically assigned to '+officer['name']+'. ' if officer else 'Complaint submitted, but no officer exists for this department; admin was notified. ')+'Ticket ID: '+tid,'success' if officer else 'error')
            return redirect(url_for('dashboard'))
    return page('''<section class="panel"><h1>File a complaint</h1><form method="post" enctype="multipart/form-data"><label>Department/category<select name="department_id" required><option value="">Select department</option>{% for d in deps %}<option value="{{d['id']}}">{{d['name']}}</option>{% endfor %}</select></label><label>Priority<select name="priority"><option>Normal</option><option>High</option></select></label><label>Description<textarea name="description" minlength="10" rows="5" required></textarea></label><label>Supporting evidence (optional, PDF/PNG/JPG, max 5 MB)<input type="file" name="attachment" accept=".pdf,.png,.jpg,.jpeg"></label><button>Submit complaint</button></form></section>''',deps=deps)
@app.route('/complaint/<ticket_id>',methods=['GET','POST'])
@require()
def complaint_detail(ticket_id):
    db=connect(); u=current_user(); c=db.execute('SELECT c.*,d.name department,s.name student_name,o.name officer FROM complaints c JOIN departments d ON d.id=c.department_id JOIN users s ON s.id=c.student_id LEFT JOIN users o ON o.id=c.assigned_officer_id WHERE c.ticket_id=?',(ticket_id,)).fetchone()
    if not c: abort(404)
    if u['role']=='student' and c['student_id']!=u['id']: abort(403)
    if u['role']=='officer' and c['assigned_officer_id']!=u['id']: abort(403)
    if u['role']=='department_head' and c['department_id']!=u['department_id']: abort(403)
    staff=db.execute("SELECT id,name,email FROM users WHERE role='officer' AND department_id=? ORDER BY name",(c['department_id'],)).fetchall()
    if request.method=='POST':
        action=request.form.get('action'); cid=c['id']
        if action=='assign' and u['role'] in ('admin','department_head'):
            oid=request.form.get('officer_id'); off=db.execute("SELECT id FROM users WHERE id=? AND role='officer' AND department_id=?",(oid,c['department_id'])).fetchone() if oid else None
            if not off: flash('Choose a valid officer from this department.','error')
            else:
                old_oid=c['assigned_officer_id']; db.execute('UPDATE complaints SET assigned_officer_id=?,updated_at=? WHERE id=?',(int(oid),now(),cid));
                if old_oid and old_oid!=int(oid): notify(old_oid,cid,'Complaint '+ticket_id+' was reassigned to another officer.');
                notify_and_email(int(oid),cid,'Complaint '+ticket_id+' has been assigned to you. Deadline: '+(c['due_at'] or 'not set'),'Complaint assigned: '+ticket_id); audit('Complaint assigned',cid,'Officer ID '+str(oid)); flash('Complaint assigned.','success')
        elif action=='escalate' and u['role']=='officer':
            reason=request.form.get('escalation_reason','').strip()
            if c['assigned_officer_id']!=u['id']: abort(403)
            if c['status'] in ('Resolved','Closed'):
                flash('Resolved or closed complaints cannot be escalated.','error')
            elif c['escalated_at']:
                flash('This complaint has already been escalated. Contact the department head for further action.','error')
            elif not c['due_at'] or c['due_at'] > now():
                flash('Escalation is available only after the complaint deadline has passed. Try again after the SLA deadline, or update the status and add progress notes.','error')
            elif len(reason)<10:
                flash('Enter an escalation reason of at least 10 characters.','error')
            else:
                db.execute('UPDATE complaints SET escalated_at=?,escalation_reason=?,updated_at=? WHERE id=?',(now(),reason,now(),cid))
                recipients={r['id'] for r in db.execute("SELECT id FROM users WHERE role='admin' OR (role='department_head' AND department_id=?)",(c['department_id'],)).fetchall()}
                for rid in recipients: notify_and_email(rid,cid,'Complaint '+ticket_id+' has been escalated by '+u['name']+'. Reason: '+reason,'Escalated complaint: '+ticket_id)
                db.execute('INSERT INTO status_history(complaint_id,user_id,old_status,new_status,remarks,created_at) VALUES(?,?,?,?,?,?)',(cid,u['id'],c['status'],c['status'],'ESCALATED: '+reason,now()))
                audit('Complaint escalated',cid,reason); flash('Complaint escalated to the department head and administrator.','success')
        elif action=='status' and u['role'] in ('admin','officer','department_head'):
            new=request.form.get('status'); remark=request.form.get('remarks','').strip()
            if new not in STATUSES: flash('Invalid status.','error')
            elif c['status']=='Closed': flash('Closed complaints are read-only.','error')
            elif u['role']=='officer' and c['assigned_officer_id']!=u['id']: abort(403)
            else:
                db.execute('UPDATE complaints SET status=?,updated_at=? WHERE id=?',(new,now(),cid)); db.execute('INSERT INTO status_history(complaint_id,user_id,old_status,new_status,remarks,created_at) VALUES(?,?,?,?,?,?)',(cid,u['id'],c['status'],new,remark,now())); db.execute('INSERT INTO resolution_notes(complaint_id,user_id,note,created_at) VALUES(?,?,?,?)',(cid,u['id'],remark or ('Status changed to '+new),now())); notify_and_email(c['student_id'],cid,'Complaint '+ticket_id+' status updated to '+new+(('. Note: '+remark) if remark else ''),'Complaint update: '+ticket_id); audit('Status changed',cid,c['status']+' -> '+new); flash('Status and tracking history updated.','success')
        elif action=='note' and u['role'] in ('admin','officer','department_head'):
            note=request.form.get('note','').strip()
            if note and c['status']!='Closed': db.execute('INSERT INTO resolution_notes(complaint_id,user_id,note,created_at) VALUES(?,?,?,?)',(cid,u['id'],note,now())); notify_and_email(c['student_id'],cid,'New update on '+ticket_id+': '+note,'Complaint note: '+ticket_id); audit('Resolution note added',cid,note); flash('Note added.','success')
            else: flash('Enter a note; closed complaints cannot be changed.','error')
        db.commit(); return redirect(url_for('complaint_detail',ticket_id=ticket_id))
    notes=db.execute('SELECT n.*,u.name author,u.role FROM resolution_notes n JOIN users u ON u.id=n.user_id WHERE n.complaint_id=? ORDER BY n.created_at DESC',(c['id'],)).fetchall(); hist=db.execute('SELECT h.*,u.name author FROM status_history h JOIN users u ON u.id=h.user_id WHERE h.complaint_id=? ORDER BY h.created_at DESC',(c['id'],)).fetchall(); can_manage=u['role'] in ('admin','department_head','officer') and c['status']!='Closed'
    return page('''<a href="{{url_for('dashboard')}}">← Dashboard</a><div class="heading"><div><h1>{{c['ticket_id']}}</h1><p>{{c['department']}} · Submitted by {{c['student_name']}} · {{c['created_at'][:16]}}</p></div><span class="status {{c['status']|lower|replace(' ','-')}}">{{c['status']}}</span></div><section class="panel"><h2>Complaint details</h2><p>{{c['description']}}</p><p><b>Priority:</b> {{c['priority']}} &nbsp; <b>Assigned officer:</b> {{c['officer'] or 'Unassigned'}}</p><p><b>Resolution deadline:</b> {{c['due_at'][:16].replace('T',' ') if c['due_at'] else 'Not set'}}{% if c['due_at'] and c['status'] not in ['Resolved','Closed'] and c['due_at'] < now_iso %} <span class="overdue">OVERDUE</span>{% endif %}</p>{% if c['escalated_at'] %}<p><b>Escalated at:</b> {{c['escalated_at'][:16].replace('T',' ')}} · <b>Reason:</b> {{c['escalation_reason']}}</p>{% endif %}{% if c['attachment_path'] %}<p>Attachment: {{c['attachment_path']}}</p>{% endif %}{% if me['role'] in ['admin','department_head'] and c['status']!='Closed' %}<form method="post" class="inline"><input type="hidden" name="action" value="assign"><label>Assign officer<select name="officer_id" required><option value="">Choose officer</option>{% for s in staff %}<option value="{{s['id']}}" {% if c['assigned_officer_id']==s['id'] %}selected{% endif %}>{{s['name']}} ({{s['email']}})</option>{% endfor %}</select></label><button>Assign / reassign</button></form>{% endif %}</section>{% if me['role']=='officer' and c['status'] not in ['Resolved','Closed'] and not c['escalated_at'] %}<section class="panel"><h2>Escalate complaint</h2><p>Escalation is enabled only after the SLA deadline passes. Add a clear reason so the department head can take over.</p><form method="post"><input type="hidden" name="action" value="escalate"><label>Reason (minimum 10 characters)<textarea name="escalation_reason" minlength="10" rows="3" required placeholder="Explain why you cannot resolve this complaint"></textarea></label><button class="secondary">Escalate to department head</button></form></section>{% endif %}{% if can_manage %}<section class="panel"><h2>Update workflow</h2><form method="post"><input type="hidden" name="action" value="status"><label>New status<select name="status">{% for s in statuses %}<option {% if c['status']==s %}selected{% endif %}>{{s}}</option>{% endfor %}</select></label><label>Progress remarks<textarea name="remarks" rows="3" placeholder="Describe the work completed or next step"></textarea></label><button>Save status update</button></form><hr><form method="post"><input type="hidden" name="action" value="note"><label>Add resolution note<textarea name="note" rows="2" required></textarea></label><button class="secondary">Add note</button></form></section>{% endif %}<div class="columns"><section class="panel"><h2>Status history</h2>{% for h in hist %}<div class="timeline"><b>{{h['new_status']}}</b><p>{{h['remarks'] or 'Status updated'}} · {{h['author']}}</p><small>{{h['created_at']}}</small></div>{% else %}<p>No history yet.</p>{% endfor %}</section><section class="panel"><h2>Resolution notes</h2>{% for n in notes %}<div class="timeline"><p>{{n['note']}}</p><small>{{n['author']}} · {{n['created_at']}}</small></div>{% else %}<p>No notes yet.</p>{% endfor %}</section></div>''',c=c,staff=staff,can_manage=can_manage,notes=notes,hist=hist)
@app.route('/staff/new',methods=['GET','POST'])
@require('admin')
def create_staff():
    db=connect(); deps=db.execute('SELECT * FROM departments ORDER BY name').fetchall()
    if request.method=='POST':
        role=request.form.get('role'); name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password',''); did=request.form.get('department_id')
        if role not in ('officer','department_head') or not name or len(pw)<8 or not email: flash('Enter valid staff details and password (minimum 8 characters).','error')
        else:
            try: db.execute('INSERT INTO users(name,email,password_hash,role,department_id,created_at) VALUES(?,?,?,?,?,?)',(name,email,generate_password_hash(pw),role,int(did) if did else None,now())); db.commit(); flash('Staff account created.','success'); return redirect(url_for('dashboard'))
            except (sqlite3.IntegrityError,ValueError): flash('Email must be unique and a valid department must be selected.','error')
    return page('''<section class="panel narrow"><h1>Create staff account</h1><form method="post"><label>Name<input name="name" required></label><label>Email<input type="email" name="email" required></label><label>Temporary password<input type="password" name="password" minlength="8" required></label><label>Role<select name="role"><option value="officer">Resolution officer</option><option value="department_head">Department head</option></select></label><label>Department<select name="department_id" required><option value="">Select department</option>{% for d in deps %}<option value="{{d['id']}}">{{d['name']}}</option>{% endfor %}</select></label><button>Create account</button></form></section>''',deps=deps)
@app.route('/notifications')
@require()
def notifications():
    db=connect(); rows=db.execute('SELECT n.*,c.ticket_id FROM notifications n JOIN complaints c ON c.id=n.complaint_id WHERE n.user_id=? ORDER BY n.created_at DESC LIMIT 100',(current_user()['id'],)).fetchall(); db.execute('UPDATE notifications SET read=1 WHERE user_id=?',(current_user()['id'],)); db.commit()
    return page('''<h1>Notifications</h1><section class="panel">{% for n in rows %}<div class="timeline"><a href="{{url_for('complaint_detail',ticket_id=n['ticket_id'])}}">{{n['ticket_id']}}</a> — {{n['message']}}<small>{{n['created_at']}}</small></div>{% else %}<p>No notifications yet.</p>{% endfor %}</section>''',rows=rows)
@app.route('/reports')
@require('admin')
def reports():
    db=connect(); total=db.execute('SELECT COUNT(*) FROM complaints').fetchone()[0]; by_status=db.execute('SELECT status,COUNT(*) n FROM complaints GROUP BY status').fetchall(); by_dept=db.execute('SELECT d.name,COUNT(c.id) n FROM departments d LEFT JOIN complaints c ON c.department_id=d.id GROUP BY d.id ORDER BY d.name').fetchall(); avg=db.execute("SELECT AVG((julianday(updated_at)-julianday(created_at))*24) FROM complaints WHERE status IN ('Resolved','Closed') AND updated_at!=''").fetchone()[0]
    return page('''<div class="heading"><div><h1>Analytics & reports</h1><p>Summary of complaint volume and resolution progress.</p></div><div class="actions"><a class="button" href="{{url_for('export_csv')}}">Export CSV</a><a class="button secondary" href="{{url_for('export_pdf')}}">Export PDF</a></div></div><div class="stats"><div class="stat"><span>Total complaints</span><strong>{{total}}</strong></div><div class="stat"><span>Avg. resolution time</span><strong>{{'%.1f'|format(avg) if avg is not none else '—'}}<small> hours</small></strong></div></div><div class="columns"><section class="panel"><h2>By status</h2>{% for x in by_status %}<p>{{x['status']}} <b class="right">{{x['n']}}</b></p>{% endfor %}</section><section class="panel"><h2>By department</h2>{% for x in by_dept %}<p>{{x['name']}} <b class="right">{{x['n']}}</b></p>{% endfor %}</section></div>''',total=total,avg=avg,by_status=by_status,by_dept=by_dept)
def report_rows(): return connect().execute('SELECT c.ticket_id,s.name student,d.name department,c.description,c.status,c.priority,o.name officer,c.created_at,c.updated_at FROM complaints c JOIN users s ON s.id=c.student_id JOIN departments d ON d.id=c.department_id LEFT JOIN users o ON o.id=c.assigned_officer_id ORDER BY c.created_at DESC').fetchall()
@app.route('/reports.csv')
@require('admin')
def export_csv():
    out=io.StringIO(); w=csv.writer(out); w.writerow(['Ticket ID','Student','Department','Description','Status','Priority','Officer','Created','Updated']); w.writerows([tuple(r) for r in report_rows()]); resp=make_response(out.getvalue()); resp.headers['Content-Type']='text/csv; charset=utf-8'; resp.headers['Content-Disposition']='attachment; filename=sccrs_report.csv'; audit('CSV report exported'); connect().commit(); return resp
@app.route('/reports.pdf')
@require('admin')
def export_pdf():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError: return 'Install reportlab with: pip install reportlab', 500
    buff=io.BytesIO(); doc=SimpleDocTemplate(buff,pagesize=landscape(A4)); styles=getSampleStyleSheet(); data=[['Ticket','Student','Department','Status','Priority','Officer','Created']]
    for r in report_rows(): data.append([str(r['ticket_id']),str(r['student'])[:18],str(r['department']),str(r['status']),str(r['priority']),str(r['officer'] or 'Unassigned')[:18],str(r['created_at'])[:10]])
    t=Table(data,repeatRows=1); t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#19324d')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.4,colors.grey),('FONTSIZE',(0,0),(-1,-1),8),('VALIGN',(0,0),(-1,-1),'TOP'),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#eef3f8')])])); doc.build([Paragraph('SCCRS Complaint Report',styles['Title']),Spacer(1,12),t]); resp=make_response(buff.getvalue()); resp.headers['Content-Type']='application/pdf'; resp.headers['Content-Disposition']='attachment; filename=sccrs_report.pdf'; audit('PDF report exported'); connect().commit(); return resp
@app.errorhandler(403)
def forbidden(e): return page('<section class="panel"><h1>Access denied</h1><p>You do not have permission to access this resource.</p></section>'),403
@app.errorhandler(404)
def not_found(e): return page('<section class="panel"><h1>Not found</h1><p>The requested page does not exist.</p></section>'),404
@app.errorhandler(413)
def too_large(e): return page('<section class="panel"><h1>File too large</h1><p>Attachments must be no larger than 5 MB.</p></section>'),413
if __name__=='__main__': init_db(); app.run(debug=os.getenv('FLASK_DEBUG','0')=='1')
