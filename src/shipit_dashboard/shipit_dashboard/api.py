# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import

import pickle
from flask import abort, request
from sqlalchemy.orm.exc import NoResultFound
from releng_common.auth import auth
from releng_common.db import db
from releng_common import log
from shipit_dashboard.helpers import gravatar
from shipit_dashboard.models import (
    BugAnalysis, BugResult, Contributor, BugContributor
)
from shipit_dashboard.serializers import (
    serialize_analysis, serialize_bug, serialize_contributor
)
from shipit_dashboard import SCOPES_USER, SCOPES_BOT, SCOPES_ADMIN


logger = log.get_logger('shipit_dashboard.api')


def ping():
    """
    Test service availability
    """
    logger.info('Got ping request. Sending pong...')
    return 'pong'


@auth.require_scopes([SCOPES_USER, SCOPES_BOT])
def list_analysis():
    """
    List all available analysis
    """
    all_analysis = BugAnalysis.with_bugs().all()
    logger.info('Fetched all analysis from db', all_analysis=all_analysis)
    return [
        serialize_analysis(analysis, nb, False)
        for analysis, nb in all_analysis
    ]


@auth.require_scopes([SCOPES_USER, SCOPES_BOT])
def get_analysis(analysis_id):
    """
    Fetch an analysis and all its bugs
    """

    # Get bug analysis
    try:
        analysis, bugs_nb = BugAnalysis.with_bugs() \
            .join(BugResult, isouter=True) \
            .filter(BugAnalysis.id == analysis_id) \
            .one()
    except NoResultFound:
        abort(404)

    logger.info('Fetched analysis from db', analysis=analysis)

    # Build JSON output
    return serialize_analysis(analysis, bugs_nb)


@auth.require_scopes(SCOPES_USER)
def update_bug(bugzilla_id):
    """
    Update a bug after modifications on Bugzilla
    """
    # Load bug
    try:
        bug = BugResult.query.filter_by(bugzilla_id=bugzilla_id).one()
    except:
        raise Exception('Missing bug {}'.format(bugzilla_id))

    # Browse changes
    payload = bug.payload_data
    for update in request.json:

        if update['target'] == 'bug':
            # Update bug flags
            if update['bugzilla_id'] != bug.bugzilla_id:
                # should never happen
                raise Exception('Invalid bugzilla_id in changes list')
            for flag_name, actions in update['changes'].items():
                payload['bug'][flag_name] = actions.get('added')

        elif update['target'] == 'attachment':
            # Build flags map
            source = update['changes'].get('flagtypes.name', {})
            removed, added = source['removed'].split(', '), source['added'].split(', ')  # noqa
            flags_map = zip(removed, added)

            def _split(fullkey):
                # From 'approval-mozilla-beta+' to
                # ('beta +', 'approval-mozilla-beta', '+')
                assert fullkey.startswith('approval-mozilla-'), \
                    '{} is not approval-mozilla-XXX'.format(fullkey)
                out = fullkey[17:]
                return out[:-1] + ' ' + out[-1], fullkey[:-1], fullkey[-1]

            # Update versions directly
            versions = payload.get('versions', {})
            for before, after in flags_map:
                before, _, _ = _split(before)
                after, name, status = _split(after)
                if before in versions:
                    versions[after] = versions[before]
                    versions[after].update({
                        'name': name,
                        'status': status,
                    })
                    del versions[before]

        else:
            raise Exception('Invalid update target {}'.format(update['target']))  # noqa

    # Save changes
    bug.payload = pickle.dumps(payload, 2)
    db.session.add(bug)
    db.session.commit()

    # Send back the bug
    return serialize_bug(bug)


@auth.require_scopes(SCOPES_BOT)
def create_bug():
    """
    Create a new bug, or update its payload
    """
    # Load bug
    bugzilla_id = request.json.get('bugzilla_id')
    if not bugzilla_id:
        raise Exception('Missing bugzilla id')
    try:
        bug = BugResult.query.filter_by(bugzilla_id=bugzilla_id).one()
        analysis_existing = [a[0] for a in bug.analysis.values('analysis_id')]
    except Exception:
        bug = BugResult(bugzilla_id=bugzilla_id)
        analysis_existing = []

    # Update bug payload
    payload = request.json.get('payload')
    payload_hash = request.json.get('payload_hash')
    if not payload or not payload_hash:
        raise Exception('Missing payload updates.')
    bug.payload = pickle.dumps(payload, 2)
    bug.payload_hash = payload_hash

    # Sync analysis in both ways:
    # * adding new bugs
    # * removing deprecated bugs
    analysis_needed = request.json.get('analysis', [])
    add = set(analysis_needed).difference(analysis_existing)
    analysis = BugAnalysis.query \
        .filter(BugAnalysis.id.in_(add)) \
        .all()
    for a in analysis:
        logger.debug('Adding new bug', analysis=a.id, bug=bug.bugzilla_id)
        a.bugs.append(bug)

    rm = set(analysis_existing).difference(analysis_needed)
    analysis = BugAnalysis.query \
        .filter(BugAnalysis.id.in_(rm)) \
        .all()
    for a in analysis:
        logger.debug('Removing old bug', analysis=a.id, bug=bug.bugzilla_id)
        a.bugs.remove(bug)

    # Save all changes
    db.session.add(bug)

    # Load users
    for user in payload.get('users', []):

        # Get or create user in db
        try:
            contrib = Contributor.query.filter_by(bugzilla_id=user['id']).one()
        except:
            contrib = Contributor(bugzilla_id=user['id'])
            contrib.name = user.get('real_name', user['name'])
            contrib.email = user['email']
            contrib.avatar_url = gravatar(user['email'])
            db.session.add(contrib)

        # Link contributor to bug
        try:
            link = BugContributor.query.filter_by(
                bug_id=bug.id,
                contributor_id=contrib.id
            ).one()
        except Exception:
            link = BugContributor(bug=bug, contributor=contrib)
        link.roles = ','.join(user['roles'])
        db.session.add(link)

    # Commit all those changes
    db.session.commit()

    # Send back the bug
    return serialize_bug(bug)


@auth.require_scopes(SCOPES_BOT)
def delete_bug(bugzilla_id):
    """
    Delete a bug when it's not in Bugzilla analysis
    """
    # Load bug
    try:
        bug = BugResult.query.filter_by(bugzilla_id=bugzilla_id).one()
    except:
        raise Exception('Missing bug {}'.format(bugzilla_id))

    bug.delete()


@auth.require_scopes(SCOPES_ADMIN)
def update_contributor(contributor_id):
    """
    Update a contributor after modifications on frontend
    """
    # Load contributor
    try:
        contributor = Contributor.query.filter_by(id=contributor_id).one()
    except:
        raise Exception('Missing contributor {}'.format(contributor_id))

    # Update karma & comment
    if 'karma' in request.json:
        contributor.karma = request.json['karma']
    if 'comment_private' in request.json:
        contributor.comment_private = request.json['comment_private']
    if 'comment_public' in request.json:
        contributor.comment_public = request.json['comment_public']

    # Commit changes
    db.session.add(contributor)
    db.session.commit()

    return serialize_contributor(contributor)
