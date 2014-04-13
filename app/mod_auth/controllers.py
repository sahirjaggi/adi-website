from app import app
import string
import random
import httplib2
from app.mod_networking.responses import response_from_json
from app.mod_auth.models import User
from app.mod_auth.forms import CreateProfileForm, AddUserForm
from mongoengine.queryset import DoesNotExist
# from pymongo.errors import DuplicateKeyError
from flask import Blueprint, render_template, request, flash, session, g, \
    redirect, url_for
from oauth2client.client import FlowExchangeError, flow_from_clientsecrets, \
    AccessTokenRefreshError, AccessTokenCredentials
from apiclient.discovery import build

mod_auth = Blueprint('auth', __name__)

gplus_service = build('plus', 'v1')


@mod_auth.before_request
def lookup_current_user():
    """Set the g.user variable to the User in the database that shares
    openid with the session, if one exists

    Note that it gets called before all requests, but not before decorators.
    """
    g.user = None
    if 'gplus_id' in session:
        gplus_id = session['gplus_id']
        try:
            g.user = User.objects.get(gplus_id=gplus_id)
        except DoesNotExist:
            pass  # Fail gracefully if the user is not in the database yet

# We have to import the login_required decorator below
# lookup_current_user() to avoid circular dependency
from app.mod_auth.decorators import login_required, development_only


@mod_auth.route('/login', methods=['GET', 'POST'])
def login():
    """If the user is not logged in, display an option to log in.  On click,
    make a request to Google to authenticate.

    If they are logged in, redirect.
    """
    if g.user is not None and 'gplus_id' in session:
        # use code=303 to avoid POSTing to the next page.
        return redirect(url_for('index'), code=303)
    load_csrf_token_into_session()
    return render_template('auth/login.html',
                           client_id=app.config["CLIENT_ID"],
                           state=session['state'],
                           # reauthorize=True,
                           next=request.args.get('next'))


@mod_auth.route('/g-plus/store-token', methods=['GET', 'POST'])
def store_token():
    if request.args.get('state', '') != session['state']:
        return response_from_json('Invalid state parameter.', 401)

    del session['state']
    code = request.data

    try:
        # Upgrade the authorization code into a credentials object
        oauth_flow = flow_from_clientsecrets('client_secrets.json', scope='')
        oauth_flow.redirect_uri = 'postmessage'
        credentials = oauth_flow.step2_exchange(code)
    except FlowExchangeError:
        return response_from_json('Failed to upgrade the authorization code.',
                                  401)

    gplus_id = credentials.id_token['sub']

    # Store the access token in the session for later use.
    session['credentials'] = credentials.access_token
    session['gplus_id'] = gplus_id

    if not User.objects(gplus_id__exists=gplus_id):

        # Get the user's name and email to populate the form
        http = httplib2.Http()
        http = credentials.authorize(http)
        people_document = gplus_service.people().get(
            userId='me').execute(http=http)

        return response_from_json(url_for(
            '.create_profile',
            next=request.args.get('next'),
            name=people_document['displayName'],
            email=people_document[
                'emails'][0]['value'],
            image_url=people_document['image']['url']), 200)

    return response_from_json(request.args.get('next'), 200)


@mod_auth.route('/create-profile', methods=['GET', 'POST'])
def create_profile():
    """Create a profile (filling in the form with openid data), and
    register it in the database.
    """
    form = CreateProfileForm(request.form,
                             name=request.args['name'],
                             email=request.args['email'],
                             next=request.args['next'])
    if form.validate_on_submit():
        if User.objects(email__exists=form.email.data):
            # A user with this email already exists.  Override it.
            user = User.objects.get(email=form.email.data)
            user.openid = session['openid']
            user.name = form.name.data
            flash('Account with this email already exists.  Overridden.')
        else:
            # Create a brand new user
            user = User(email=form.email.data,
                        name=form.name.data,
                        gplus_id=session['gplus_id'])
            flash('Account created successfully.')

        user.save()

        # use code=303 to avoid POSTing to the next page.
        return redirect(form.next.data, code=303)

    return render_template('auth/create_profile.html',
                           image_url=request.args.get('image_url'), form=form)


@mod_auth.route('/remove/<user_email>')
@login_required
def remove(user_email):
    """Remove the user with the specified email from the database.  If
    attempting to remove user that is currently logged in, do so and then
    log out.
    """
    user = User.objects.get(email=user_email)
    if user.gplus_id == session['gplus_id']:
        user.delete()
        return redirect(url_for('.logout'))
    user.delete()
    return redirect(url_for('.view_users'))


@mod_auth.route('/logout')
def logout():
    """Logs the user out"""
    session.pop('gplus_id', None)
    g.user = None
    flash(u'You were signed out')
    return redirect('http://adicu.com')


@mod_auth.route('/adduser', methods=['GET', 'POST'])
@login_required
def add_user():
    # TODO: use the next variable if user was redirected to login
    form = AddUserForm(request.form)
    if form.validate_on_submit():
        # Register a new user in the database
        new_user = User(name="CHANGE ME", email=form.email.data)
        new_user.save()
    return render_template('auth/add_user.html')


def load_csrf_token_into_session():
    """Create a unique session cross-site request forgery (CSRF) token and
    load it into the session for later verification.
    """
    state = ''.join(random.choice(string.ascii_uppercase + string.digits)
                    for x in xrange(32))
    session['state'] = state


@mod_auth.route('/disconnect', methods=['GET', 'POST'])
def disconnect():
    """Revoke current user's token and reset their session."""
    # Only disconnect a connected user.
    credentials = AccessTokenCredentials(
        session.get('credentials'), request.headers.get('User-Agent'))
    if credentials is None:
        return response_from_json('Current user not connected.', 401)

    # Execute HTTP GET request to revoke current token.
    access_token = credentials.access_token
    url = 'https://accounts.google.com/o/oauth2/revoke?token=%s' % \
        str(access_token)
    h = httplib2.Http()
    result = h.request(url, 'GET')[0]

    if result['status'] == '200':
        # Reset the user's session.
        del session['credentials']

        # use code=303 to avoid POSTing to the next page.
        return redirect(url_for('index'), code=303)
    else:
        # For whatever reason, the given token was invalid.
        return response_from_json('Failed to revoke token for given user.',
                                  400)


@mod_auth.route('/people', methods=['GET'])
def people():
    """Get list of people user has shared with this app."""
    credentials = AccessTokenCredentials(
        session.get('credentials'), request.headers.get('User-Agent'))
    # Only fetch a list of people for connected users.
    if credentials is None:
        return response_from_json('Current user not connected.', 401)
    try:
        # Create a new authorized API client.
        http = httplib2.Http()
        http = credentials.authorize(http)
        # Get a list of people that this user has shared with this app.
        google_request = gplus_service.people().list(
            userId='me', collection='visible')
        result = google_request.execute(http=http)

        return response_from_json(result, 200)
    except AccessTokenRefreshError:
        return response_from_json('Failed to refresh access token.', 500)


#============================================================
# Development Only (quick and dirty ways to play with Users)
#============================================================


@mod_auth.route('/become/<level>')
@development_only
@login_required
def become(level=0):
    """Change the privelages of the logged in user.

    level -- 1: Editor, 2: Publisher, 3: Admin
    """
    level = int(level)
    admin_privelages = {
        "edit": level > 0,
        "publish": level > 1,
        "admin": level > 2
    }
    db_dict = dict((("set__privelages__%s" % k, v)
                   for k, v in admin_privelages.iteritems()))
    User.objects(openid=session['openid']).update(**db_dict)
    return redirect(url_for('.view_users'))


@mod_auth.route('/super')
@development_only
@login_required
def super():
    """Special case of become()"""
    return redirect(url_for('.become', level=3))


@mod_auth.route('/view-users')
@development_only
def view_users():
    """Print out all the users"""
    return str(User.objects)


@mod_auth.route('/wipe')
@development_only
def wipe():
    """Wipe all users from the database"""
    if request.method == "POST":
        User.objects.drop_collection()
        return redirect(url_for('.view_users'))
    return '''<form action="/login" method=post>
        <input type=submit value="Wipe the Database">
        </form>'''


@mod_auth.route('/session')
@development_only
def view_session():
    return str(session)
