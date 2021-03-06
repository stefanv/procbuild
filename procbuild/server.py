from procbuild import (app, log, papers, pr_info, paper_queue,
                       MASTER_BRANCH, ALLOW_MANUAL_BUILD_TRIGGER)

from flask import (render_template, url_for, send_file, jsonify,
                   request)
import json
import os
from os.path import join as joinp
from glob import glob
from futil import age as file_age, base_path
import time

from builder import build as build_paper, cache
from multiprocessing import Process
from pr_list import update_papers, pr_list_file


def status_file(nr):
    return joinp(cache(), str(nr) + '.status')


def status_from_cache(nr):
    if nr == '*':
        status_files = [status_file(i) for i in range(len(papers))]
    else:
        status_files = [status_file(nr)]

    data = {}

    for fn in status_files:
        n = fn.split('/')[-1].split('.')[0]

        try:
            papers[int(n)]
        except:
            data[n] = {'status': 'fail',
                       'data': {'build_output': 'Invalid paper'}}
        else:
            status = {'status': 'fail',
                      'data': {'build_output': 'No build info'}}

            if os.path.exists(fn):
                with open(fn, 'r') as f:
                    try:
                        data[n] = json.load(f)
                    except ValueError:
                        pass

    # Unpack status if only one record requested
    if nr != '*':
        return data[nr]
    else:
        return data


@app.route('/')
def index():
    prs_age = file_age(pr_list_file)
    if (prs_age is None or prs_age > 60):
        log("Updating papers...")
        update_papers()

    return render_template('index.html', papers=papers,
                           build_url=url_for('build', nr=''),
                           download_url=url_for('download', nr=''),
                           allow_manual_build_trigger=ALLOW_MANUAL_BUILD_TRIGGER)


def _process_queue(queue):
    done = False
    while not done:
        nr = queue.get()
        if nr is None:
            log("Sentinel found in queue. Ending queue monitor.")
            done = True
        else:
            log("Queue yielded paper #%d. Left: %d" % (nr, queue.qsize()))
            _build_worker(nr)

def monitor_queue():
    print "Launching queue monitoring process..."
    p = Process(target=_process_queue, kwargs=dict(queue=paper_queue))
    p.start()


def dummy_build(nr):
        return jsonify({'status': 'fail', 'message': 'Not authorized'})

def real_build(nr):
    try:
        pr = pr_info[int(nr)]
    except:
        return jsonify({'status': 'fail',
                        'message': 'Invalid paper specified'})

    if paper_queue.qsize() >= 50:
        return jsonify({'status': 'fail',
                        'message': 'Build queue is currently full.'})

    paper_queue.put(int(nr))

    return jsonify({'status': 'success',
                    'data': {'info': 'Build for paper %s scheduled.  Note that '
                                     'builds are only executed if the current '
                                     'build attempt is more than '
                                     '5 minutes old.' % nr}})


@app.route('/build/<nr>')
def build(*args, **kwarg):
    if ALLOW_MANUAL_BUILD_TRIGGER:
        return real_build(*args, **kwarg)
    else:
        return dummy_build(*args, **kwarg)


def _build_worker(nr):
    pr = pr_info[int(nr)]

    age = file_age(status_file(nr))
    if not (age is None or age > 2):
        log("Did not build paper %d--recently built." % nr)
        return

    status_log = status_file(nr)
    with open(status_log, 'w') as f:
        json.dump({'status': 'fail',
                   'data': {'build_status': 'Building...',
                            'build_output': 'Initializing build...',
                            'build_timestamp': ''}}, f)


    def build_and_log(*args, **kwargs):
        status = build_paper(*args, **kwargs)
        with open(status_log, 'w') as f:
            json.dump(status, f)

    p = Process(target=build_and_log,
                kwargs=dict(user=pr['user'], branch=pr['branch'],
                            master_branch=MASTER_BRANCH,
                            target=nr, log=log))
    p.start()

    def killer(process, timeout):
        time.sleep(timeout)
        try:
            process.terminate()
        except OSError:
            pass

    k = Process(target=killer, args=(p, 180))
    k.start()

    # Wait for process to complete or to be killed
    p.join()
    k.terminate()


@app.route('/status')
@app.route('/status/<nr>')
def status(nr=None):
    data = []

    if nr is None:
        nr = '*'

    return jsonify(status_from_cache(nr))


@app.route('/download/<nr>')
def download(nr):
    status = status_from_cache(nr)

    if not (status.get('data', {}).get('build_status', '') == 'success'):
        return "Paper has not been successfully rendered yet."

    return send_file(status['data']['build_pdf_path'])


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
    except:
        return jsonify({'status': 'fail',
                        'message': 'Invalid JSON data'})

    pr_url = data.get('pull_request', {}).get('html_url', '')
    paper = [p for p, info in papers if info['url'] == pr_url]

    if paper:
        return real_build(paper[0])
    else:
        return jsonify({'status': 'fail',
                        'message': 'Hook called for building '
                                   'non-existing paper (%s)' % pr_url})
