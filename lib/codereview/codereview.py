#!/usr/bin/env python
#
# Copyright 2007-2009 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''Mercurial interface to codereview.appspot.com.

To configure it, set the following options in
your repository's .hg/hgrc file.

    [extensions]
    codereview = path/to/codereview.py

    [codereview]
    project = project-url        # optional

If the project URL is specified, codereview will fetch
default the reviewer and cc list from that URL each time
it runs an "upload" command.
'''

from mercurial import cmdutil, commands, hg, util, error, match
from mercurial.node import nullrev, hex, nullid, short
import os, re
import stat
from HTMLParser import HTMLParser

try:
	hgversion = util.version()
except Exception, e:
	from mercurial.version import version as v
	hgversion = v.get_version()


# To experiment with Mercurial in the python interpreter: 
#    >>> repo = hg.repository(ui.ui(), path = ".")

#######################################################################
# Normally I would split this into multiple files, but it simplifies
# import path headaches to keep it all in one file.  Sorry.

import sys
if __name__ == "__main__":
	print >>sys.stderr, "This is a Mercurial extension and should not be invoked directly."
	sys.exit(2)


#######################################################################
# Change list parsing.
#
# Change lists are stored in .hg/codereview/cl.nnnnnn
# where nnnnnn is the number assigned by the code review server.
# Most data about a change list is stored on the code review server
# too: the description, reviewer, and cc list are all stored there.
# The only thing in the cl.nnnnnn file is the list of relevant files.
# Also, the existence of the cl.nnnnnn file marks this repository
# as the one where the change list lives.

class CL(object):
	def __init__(self, name):
		self.name = name
		self.desc = ''
		self.files = []
		self.reviewer = []
		self.cc = []
		self.url = ''
		self.local = False
		self.web = False

	def DiskText(self):
		cl = self
		s = ""
		s += "Description:\n"
		s += Indent(cl.desc, "\t")
		s += "Files:\n"
		for f in cl.files:
			s += "\t" + f + "\n"
		return s

	def EditorText(self):
		cl = self
		s = _change_prolog
		s += "\n"
		if cl.url != '':
			s += 'URL: ' + cl.url + '	# cannot edit\n\n'
		s += "Reviewer: " + JoinComma(cl.reviewer) + "\n"
		s += "CC: " + JoinComma(cl.cc) + "\n"
		s += "\n"
		s += "Description:\n"
		if cl.desc == '':
			s += "\t<enter description here>\n"
		else:
			s += Indent(cl.desc, "\t")
		s += "\n"
		s += "Files:\n"
		for f in cl.files:
			s += "\t" + f + "\n"
		s += "\n"
		return s
	
	def PendingText(self):
		cl = self
		s = cl.name + ":" + "\n"
		s += Indent(cl.desc, "\t")
		s += "\n"
		s += "\tReviewer: " + JoinComma(cl.reviewer) + "\n"
		s += "\tCC: " + JoinComma(cl.cc) + "\n"
		s += "\tFiles:\n"
		for f in cl.files:
			s += "\t\t" + f + "\n"
		return s
	
	def Flush(self, ui, repo):
		if self.name == "new":
			self.Upload(ui, repo)
		dir = CodeReviewDir(ui, repo)
		path = dir + '/cl.' + self.name
		f = open(path+'!', "w")
		f.write(self.DiskText())
		f.close()
		os.rename(path+'!', path)
		if self.web:
			EditDesc(self.name, subject=line1(self.desc), desc=self.desc,
				reviewers=JoinComma(self.reviewer), cc=JoinComma(self.cc))
	
	def Delete(self, ui, repo):
		dir = CodeReviewDir(ui, repo)
		os.unlink(dir + "/cl." + self.name)

	def Upload(self, ui, repo, send_mail=False):
		os.chdir(repo.root)
		form_fields = [
			("content_upload", "1"),
			("reviewers", JoinComma(self.reviewer)),
			("cc", JoinComma(self.cc)),
			("description", self.desc),
			("base_hashes", ""),
			("subject", line1(self.desc)),
		]

		# NOTE(rsc): This duplicates too much of RealMain,
		# but RealMain doesn't have the nicest interface in the world.
		if self.name != "new":
			form_fields.append(("issue", self.name))
		vcs = GuessVCS(upload_options)
		data = vcs.GenerateDiff(self.files)
		files = vcs.GetBaseFiles(data)
		if len(data) > MAX_UPLOAD_SIZE:
			uploaded_diff_file = []
			form_fields.append(("separate_patches", "1"))
		else:
			uploaded_diff_file = [("data", "data.diff", data)]
		ctype, body = EncodeMultipartFormData(form_fields, uploaded_diff_file)
		response_body = MySend("/upload", body, content_type=ctype)
		patchset = None
		msg = response_body
		lines = msg.splitlines()
		if len(lines) >= 2:
			msg = lines[0]
			patchset = lines[1].strip()
			patches = [x.split(" ", 1) for x in lines[2:]]
		ui.status("uploaded: " + msg + "\n")
		if not response_body.startswith("Issue created.") and not response_body.startswith("Issue updated."):
			print response_body
			raise "failed to update issue"
		issue = msg[msg.rfind("/")+1:]
		self.name = issue
		if not uploaded_diff_file:
			patches = UploadSeparatePatches(issue, rpc, patchset, data, upload_options)
		vcs.UploadBaseFiles(issue, rpc, patches, patchset, upload_options, files)
		if send_mail:
			MySend("/" + issue + "/mail", payload="")
		self.web = True
		self.Flush(ui, repo)
		return

def GoodCLName(name):
	return re.match("^[0-9]+$", name)	

def ParseCL(text, name):
	sname = None
	lineno = 0
	sections = {
		'Description': '',
		'Files': '',
		'URL': '',
		'Reviewer': '',
		'CC': '',
	}
	for line in text.split('\n'):
		lineno += 1
		line = line.rstrip()
		if line != '' and line[0] == '#':
			continue
		if line == '' or line[0] == ' ' or line[0] == '\t':
			if sname == None and line != '':
				return None, lineno, 'text outside section'
			if sname != None:
				sections[sname] += line + '\n'
			continue
		p = line.find(':')
		if p >= 0:
			s, val = line[:p].strip(), line[p+1:].strip()
			if s in sections:
				sname = s
				if val != '':
					sections[sname] += val + '\n'
				continue
		return None, lineno, 'malformed section header'

	for k in sections:
		sections[k] = StripCommon(sections[k]).rstrip()

	cl = CL(name)
	cl.desc = sections['Description']
	for line in sections['Files'].split('\n'):
		i = line.find('#')
		if i >= 0:
			line = line[0:i].rstrip()
		if line == '':
			continue
		cl.files.append(line)
	cl.reviewer = SplitCommaSpace(sections['Reviewer'])
	cl.cc = SplitCommaSpace(sections['CC'])
	cl.url = sections['URL']
	if cl.desc == '<enter description here>':
		cl.desc = '';
	return cl, 0, ''

def SplitCommaSpace(s):
	return s.replace(",", " ").split()

def JoinComma(l):
	return ", ".join(l)

# Load CL from disk and/or the web.
def LoadCL(ui, repo, name, web=True):
	if not GoodCLName(name):
		return None, "invalid CL name"
	dir = CodeReviewDir(ui, repo)
	path = dir + "cl." + name
	try:
		ff = open(path)
		text = ff.read()
		ff.close()
		cl, lineno, err = ParseCL(text, name)
		if err != "":
			return None, "malformed CL data"
		cl.local = True
	except Exception, e:
		cl = CL(name)
	if web:
		try:
			f = GetSettings(name)
		except Exception, e:
			return None, "cannot load CL data from code review server"
		cl.reviewer = SplitCommaSpace(f['reviewers'])
		cl.cc = SplitCommaSpace(f['cc'])
		cl.desc = f['description']
		cl.url = server_url_base + name
		cl.web = True
	return cl, ''

# Load all the CLs from this repository.
def LoadAllCL(ui, repo, web=True):
	dir = CodeReviewDir(ui, repo)
	m = {}
	for f in os.listdir(dir):
		if f.startswith('cl.'):
			cl, err = LoadCL(ui, repo, f[3:], web=web)
			if err != '':
				ui.warn("loading "+dir+f+": " + err + "\n")
				continue
			m[cl.name] = cl
	return m

# Find repository root.  On error, ui.warn and return None
def RepoDir(ui, repo):
	url = repo.url();
	if not url.startswith('file:/'):
		ui.warn("repository %s is not in local file system\n" % (url,))
		return None
	url = url[5:]
	if url.endswith('/'):
		url = url[:-1]
	return url

# Find (or make) code review directory.  On error, ui.warn and return None
def CodeReviewDir(ui, repo):
	dir = RepoDir(ui, repo)
	if dir == None:
		return None
	dir += '/.hg/codereview/'
	if not os.path.isdir(dir):
		try:
			os.mkdir(dir, 0700)
		except Exception, e:
			ui.warn('cannot mkdir %s: %s\n' % (dir, e))
			return None
	return dir

# Strip maximal common leading white space prefix from text
def StripCommon(text):
	ws = None
	for line in text.split('\n'):
		line = line.rstrip()
		if line == '':
			continue
		white = line[:len(line)-len(line.lstrip())]
		if ws == None:
			ws = white
		else:
			common = ''
			for i in range(min(len(white), len(ws))+1):
				if white[0:i] == ws[0:i]:
					common = white[0:i]
			ws = common
		if ws == '':
			break
	if ws == None:
		return text
	t = ''
	for line in text.split('\n'):
		line = line.rstrip()
		if line.startswith(ws):
			line = line[len(ws):]
		if line == '' and t == '':
			continue
		t += line + '\n'
	while len(t) >= 2 and t[-2:] == '\n\n':
		t = t[:-1]
	return t

# Indent text with indent.
def Indent(text, indent):
	t = ''
	for line in text.split('\n'):
		t += indent + line + '\n'
	return t

# Return the first line of l
def line1(text):
	return text.split('\n')[0]

_change_prolog = """# Change list.
# Lines beginning with # are ignored.
# Multi-line values should be indented.
"""

#######################################################################
# Mercurial helper functions

# Return list of changed files in repository that match pats.
def ChangedFiles(ui, repo, pats, opts):
	# Find list of files being operated on.
	# TODO(rsc): The cutoff might not be 1.3.
	# Definitely after 1.0.2.
	try:
		matcher = cmdutil.match(repo, pats, opts)
		node1, node2 = cmdutil.revpair(repo, None)
		modified, added, removed = repo.status(node1, node2, matcher)[:3]
	except AttributeError, e:
		# Probably in earlier Mercurial, say 1.0.2.
		_, matcher, _ = cmdutil.matchpats(repo, pats, opts)
		node1, node2 = cmdutil.revpair(repo, None)
		modified, added, removed = repo.status(node1, node2, match=matcher)[:3]
	return modified + added + removed

# Return list of files claimed by existing CLs
def TakenFiles(ui, repo):
	all = LoadAllCL(ui, repo, web=False)
	taken = {}
	for _, cl in all.items():
		for f in cl.files:
			taken[f] = cl
	return taken

# Return list of changed files that are not claimed by other CLs
def DefaultFiles(ui, repo, pats, opts):
	return Sub(ChangedFiles(ui, repo, pats, opts), TakenFiles(ui, repo))

def Sub(l1, l2):
	return [l for l in l1 if l not in l2]

def Add(l1, l2):
	return l1 + Sub(l2, l1)

def Intersect(l1, l2):
	return [l for l in l1 if l in l2]

def Incoming(ui, repo, opts, op):
	source, _, _ = hg.parseurl(ui.expandpath("default"), None)
	try:
		other = hg.repository(cmdutil.remoteui(repo, opts), source)
		_, incoming, _ = repo.findcommonincoming(other)
	except AttributeError, e:
		other = hg.repository(ui, source)
		incoming = repo.findincoming(other)
	return incoming

def EditCL(ui, repo, cl):
	s = cl.EditorText()
	while True:
		s = ui.edit(s, ui.username())
		clx, line, err = ParseCL(s, cl.name)
		if err != '':
			# TODO(rsc): another 1.3 inconsistency
			if ui.prompt("error parsing change list: line %d: %s\nre-edit (y/n)?" % (line, err), ["&yes", "&no"], "y") == "n":
				return "change list not modified"
			continue
		cl.desc = clx.desc;
		cl.reviewer = clx.reviewer
		cl.cc = clx.cc
		cl.files = clx.files
		if cl.desc == '':
			if ui.prompt("change list should have description\nre-edit (y/n)?", ["&yes", "&no"], "y") != "n":
				continue
		break
	return ""

# For use by submit, etc. (NOT by change)
# Get change list number or list of files from command line.
# If files are given, make a new change list.
def CommandLineCL(ui, repo, pats, opts):
	if len(pats) > 0 and GoodCLName(pats[0]):
		if len(pats) != 1:
			return None, "cannot specify change number and file names"
		if opts.get('message'):
			return None, "cannot use -m with existing CL"
		cl, err = LoadCL(ui, repo, pats[0], web=True)
	else:
		cl = CL("new")
		cl.local = True
		cl.files = Sub(ChangedFiles(ui, repo, pats, opts), TakenFiles(ui, repo))
		if not cl.files:
			return None, "no files changed"
	if opts.get('reviewer'):
		cl.reviewer = Add(cl.reviewer, SplitCommaSpace(opts.get('reviewer')))
	if opts.get('cc'):
		cl.cc = Add(cl.cc, SplitCommaSpace(opts.get('cc')))
	if cl.name == "new":
		if opts.get('message'):
			cl.desc = opts.get('message')
		else:
			err = EditCL(ui, repo, cl)
			if err != '':
				return None, err
	return cl, ""

#######################################################################
# Mercurial commands

# until done debugging
server = "localhost:1"
# server = "codereview.appspot.com"

server_url_base = None

# every command must take a ui and and repo as arguments.
# opts is a dict where you can find other command line flags
#
# Other parameters are taken in order from items on the command line that
# don't start with a dash.  If no default value is given in the parameter list,
# they are required.
# 

# Change command.
def change(ui, repo, *pats, **opts):
	"""create or edit a change list
	
	Create or edit a change list.
	A change list is a group of files to be reviewed and submitted together,
	plus a textual description of the change.
	Change lists are referred to by simple alphanumeric names.

	Changes must be reviewed before they can be submitted.
	
	In the absence of options, the change command opens the
	change list for editing in the default editor.  
	"""
	
	if opts["add"] and opts["delete"]:
		return "cannot use -a with -d"

	if (opts["add"] or opts["delete"]) and (opts["stdin"] or opts["stdout"]):
		return "cannot use -a/-d with -i/-o"

	dirty = {}
	if len(pats) > 0 and GoodCLName(pats[0]):
		name = pats[0]
		pats = pats[1:]
		cl, err = LoadCL(ui, repo, name, web=True)
		if err != '':
			return err
		if not cl.local and (opts["add"] or opts["delete"] or opts["stdin"] or not opts["stdout"]):
			return "cannot change non-local CL " + name
	else:
		if opts["add"] or opts["delete"]:
			return "cannot use -a/-d when creating CL"
		name = "new"
		cl = CL("new")
		dirty[cl] = True
	
	files = ChangedFiles(ui, repo, pats, opts)
	taken = TakenFiles(ui, repo)
	files = Sub(files, taken)

	if opts["stdin"]:
		s = sys.stdin.read()
		clx, line, err = ParseCL(s, name)
		if err != '':
			return "error parsing change list: line %d: %s" % (line, err)
		if clx.desc is not None:
			cl.desc = clx.desc;
			dirty[cl] = True
		if clx.reviewer is not None:
			cl.reviewer = clx.reviewer
			dirty[cl] = True
		if clx.cc is not None:
			cl.cc = clx.cc
			dirty[cl] = True
		if clx.files is not None:
			cl.files = clx.files
			dirty[cl] = True

	if opts["add"]:
		newfiles = Sub(files, cl.files)
		stolen = Intersect(newfiles, taken)
		if stolen:
			ui.status("# Taking files from other CLs.  To undo:\n")
			for f in stolen:
				ocl = taken[f]
				ui.status("#	hg change -a %s %s\n" % (ocl.name, f))
				ocl.files = Sub(ocl.files, [f])
				dirty[ocl] = True
		not_stolen = Sub(newfiles, stolen)
		if not_stolen:
			ui.status("# Add files to CL.  To undo:\n")
			for f in not_stolen:
				ui.status("#	hg change -d %s %s\n" % (cl.name, f))
		if newfiles:
			cl.files += newfiles
			dirty[cl] = True

	if opts["delete"]:
		oldfiles = Intersect(files, cl.files)
		if oldfiles:
			ui.status("# Removing files from CL.  To undo:\n")
			for f in oldfiles:
				ui.status("#	hg change -a %s %s\n" % (cl.name, f))
			cl.files = Sub(cl.files, oldfiles)
			dirty[cl] = True

	if not opts["add"] and not opts["delete"] and not opts["stdin"] and not opts["stdout"]:
		if name == "new":
			cl.files = files
		err = EditCL(ui, repo, cl)
		if err != "":
			return err
		dirty[cl] = True

	for d, _ in dirty.items():
		d.Flush(ui, repo)
	
	if opts["stdout"]:
		ui.write(cl.EditorText())
	elif name == "new":
		if ui.quiet:
			ui.write(cl.name)
		else:
			ui.write("URL: " + cl.url)
	return

def pending(ui, repo, *pats, **opts):
	m = LoadAllCL(ui, repo, web=True)
	names = m.keys()
	names.sort()
	for name in names:
		cl = m[name]
		ui.write(cl.PendingText() + "\n")

	files = DefaultFiles(ui, repo, [], opts)
	if len(files) > 0:
		s = "Changed files not in any CL:\n"
		for f in files:
			s += "\t" + f + "\n"
		ui.write(s)

def upload(ui, repo, name, **opts):
	repo.ui.quiet = True
	cl, err = LoadCL(ui, repo, name, web=True)
	if err != "":
		return err
	if not cl.local:
		return "cannot upload non-local change"
	cl.Upload(ui, repo)
	print "%s%s\n" % (server_url_base, cl.name)
	return

def mail(ui, repo, *pats, **opts):
	cl, err = CommandLineCL(ui, repo, pats, opts)
	if err != "":
		return err
	if not cl.reviewer:
		return "no reviewers listed in CL"
	cl.Upload(ui, repo)
	pmsg = "Hello " + JoinComma(cl.reviewer) + ",\n"
	pmsg += "\n"
	pmsg += "I'd like you to review the following change.\n"
	PostMessage(cl.name, pmsg, send_mail="checked", subject="code review: " + line1(cl.desc))
	
def submit(ui, repo, *pats, **opts):
	"""submit change to remote repository
	
	Submits change to remote repository.
	Bails out if the local repository is not in sync with the remote one.
	"""
	repo.ui.quiet = True
	if not opts["no_incoming"] and Incoming(ui, repo, opts, "submit"):
		return "local repository out of date; must sync before submit"

	cl, err = CommandLineCL(ui, repo, pats, opts)
	if err != "":
		return err
	
	about = ""
	if cl.reviewer:
		about += "R=" + JoinComma(cl.reviewer) + "\n"
	if opts.get('tbr'):
		tbr = SplitCommaSpace(opts.get('tbr'))
		cl.reviewer = Add(cl.reviewer, tbr)
		about += "TBR=" + JoinComma(tbr) + "\n"
	if cl.cc:
		about += "CC=" + JoinComma(cl.cc) + "\n"

	if not cl.reviewer:
		return "no reviewers listed in CL"

	if not cl.local:
		return "cannot submit non-local CL"

	# upload, to sync current patch and also get change number if CL is new.
	cl.Upload(ui, repo)
	about += "%s%s\n" % (server_url_base, cl.name)

	# submit changes locally
	date = opts.get('date')
	if date:
		opts['date'] = util.parsedate(date)
	opts['message'] = cl.desc.rstrip() + "\n\n" + about
	try:
		m = match.exact(repo.root, repo.getcwd(), cl.files)
		node = repo.commit(opts['message'], opts.get('user'), opts.get('date'), m)
	except Exception, e:
		_, m, _ = util._matcher(repo.root, repo.getcwd(), cl.files, None, None, 'path', None)
		node = repo.commit(text=opts['message'], user=opts.get('user'), date=opts.get('date'), match=m)
	if not node:
		return "nothing changed"

	log = repo.changelog
	rev = log.rev(node)
	parents = log.parentrevs(rev)
	if (rev-1 not in parents and
			(parents == (nullrev, nullrev) or
			len(log.heads(log.node(parents[0]))) > 1 and
			(parents[1] == nullrev or len(log.heads(log.node(parents[1]))) > 1))):
		repo.rollback()
		return "local repository out of date (created new head); must sync before submit"

	# push changes to remote.
	# if it works, we're committed.
	# if not, roll back
	dest, _, _ = hg.parseurl(ui.expandpath("default"), None)
	try:
		other = hg.repository(cmdutil.remoteui(repo, opts), dest)
	except AttributeError, e:
		other = hg.repository(ui, dest)
	r = repo.push(other, False, None)
	if r == 0:
		repo.rollback()
		return "local repository out of date; must sync before submit"

	# we're committed. upload final patch, close review, add commit message
	changeURL = short(node)
	url = other.url()
	m = re.match("^https?://([^@/]+@)?([^.]+)\.googlecode\.com/hg/", url)
	if m:
		changeURL = "http://code.google.com/p/%s/source/detail?r=%s" % (m.group(2), changeURL)
	else:
		print >>sys.stderr, "URL: ", url
	pmsg = "*** Submitted as " + changeURL + " ***\n\n" + opts['message']
	PostMessage(cl.name, pmsg, send_mail="checked")
	EditDesc(cl.name, closed="checked")
	cl.Delete(ui, repo)

def sync(ui, repo, **opts):
	"""synchronize with remote repository
	
	Incorporates recent changes from the remote repository
	into the local repository.
	
	Equivalent to the Mercurial command "hg pull -u".
	"""
	repo.ui.quiet = True
	source, _, _ = hg.parseurl(ui.expandpath("default"), None)
	try:
		other = hg.repository(cmdutil.remoteui(repo, opts), source)
	except AttributeError, e:
		other = hg.repository(ui, source)
	modheads = repo.pull(other)
	return commands.postincoming(ui, repo, modheads, True, "tip")

def uisetup(ui):
	if "^commit|ci" in commands.table:
		commands.table["^commit|ci"] = (nocommit, [], "")
	RietveldSetup(ui)

def nocommit(ui, repo, *pats, **opts):
	return "The codereview extension is enabled; do not use commit."

review_opts = [
	('r', 'reviewer', '', 'add reviewer'),
	('', 'cc', '', 'add cc'),
	('', 'tbr', '', 'add future reviewer'),
	('m', 'message', '', 'change description (for new change)'),
]

cmdtable = {
	# The ^ means to show this command in the help text that
	# is printed when running hg with no arguments.

	# TODO: Should change upload?
	"^change": (
		change,
		[
			('a', 'add', None, 'add files to change list'),
			('d', 'delete', None, 'remove files from change list'),
			('o', 'stdout', None, 'print change list to standard output'),
			('i', 'stdin', None, 'read change list from standard input'),
		],
		"[-a | -d | [-i] [-o]] [change#] [FILE ...]"
	),
	"^pending|p": (
		pending,
		[],
		"[FILE ...]"
	),

	# TODO: cdiff - steal diff options and command line

	"^upload": (
		upload,
		[],
		"change#"
	),
	
	"^mail": (
		mail,
		review_opts + [
		] + commands.walkopts,
		"[-r reviewer] [--cc cc] [change# | file ...]"
	),

	"^submit": (
		submit,
		review_opts + [
			('', 'no_incoming', None, 'disable initial incoming check (for testing)'),
		] + commands.walkopts + commands.commitopts + commands.commitopts2,
		"[-r reviewer] [--cc cc] [change# | file ...]"
	),
	
	"^sync": (
		sync,
		[],
		"",
	),
	
	"commit|ci": (
		nocommit,
		[],
		"",
	),
}


#######################################################################
# Wrappers around upload.py for interacting with Rietveld

emptydiff = """Index: ~rietveld~placeholder~
===================================================================
diff --git a/~rietveld~placeholder~ b/~rietveld~placeholder~
new file mode 100644
"""

# HTML form parser
class FormParser(HTMLParser):
	def __init__(self):
		self.map = {}
		self.curtag = None
		self.curdata = None
		HTMLParser.__init__(self)
	def handle_starttag(self, tag, attrs):
		if tag == "input":
			key = None
			value = ''
			for a in attrs:
				if a[0] == 'name':
					key = a[1]
				if a[0] == 'value':
					value = a[1]
			if key is not None:
				self.map[key] = value
		if tag == "textarea":
			key = None
			for a in attrs:
				if a[0] == 'name':
					key = a[1]
			if key is not None:
				self.curtag = key
				self.curdata = ''
	def handle_endtag(self, tag):
		if tag == "textarea" and self.curtag is not None:
			self.map[self.curtag] = self.curdata
			self.curtag = None
			self.curdata = None
	def handle_charref(self, name):
		import unicodedata
		char = unicodedata.name(unichr(int(name)))
		self.handle_data(char)
	def handle_entityref(self, name):
		import htmlentitydefs
		if name in htmlentitydefs.entitydefs:
			self.handle_data(htmlentitydefs.entitydefs[name])
		else:
			self.handle_data("&" + name + ";")
	def handle_data(self, data):
		if self.curdata is not None:
			self.curdata += data

# Like upload.py Send but only authenticates when the 
# redirect is to www.google.com/accounts.  This keeps
# unnecessary redirects from happening during testing.
def MySend(request_path, payload=None,
           content_type="application/octet-stream",
           timeout=None,
           **kwargs):
    """Sends an RPC and returns the response.

    Args:
      request_path: The path to send the request to, eg /api/appversion/create.
      payload: The body of the request, or None to send an empty request.
      content_type: The Content-Type header to use.
      timeout: timeout in seconds; default None i.e. no timeout.
        (Note: for large requests on OS X, the timeout doesn't work right.)
      kwargs: Any keyword arguments are converted into query string parameters.

    Returns:
      The response body, as a string.
    """
    # TODO: Don't require authentication.  Let the server say
    # whether it is necessary.
    global rpc
    if rpc == None:
    	rpc = GetRpcServer(upload_options)
    self = rpc
    if not self.authenticated:
      self._Authenticate()

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
      tries = 0
      while True:
        tries += 1
        args = dict(kwargs)
        url = "http://%s%s" % (self.host, request_path)
        if args:
          url += "?" + urllib.urlencode(args)
        req = self._CreateRequest(url=url, data=payload)
        req.add_header("Content-Type", content_type)
        try:
          f = self.opener.open(req)
          response = f.read()
          f.close()
          return response
        except urllib2.HTTPError, e:
          if tries > 3:
            raise
          elif e.code == 401:
            self._Authenticate()
          elif e.code == 302:
            loc = e.info()["location"]
            if not loc.startswith('https://www.google.com/accounts/ServiceLogin'):
              return ''
            self._Authenticate()
          else:
            raise
    finally:
      socket.setdefaulttimeout(old_timeout)

def GetForm(url):
	f = FormParser()
	f.feed(MySend(url))
	f.close()
	for k,v in f.map.items():
		f.map[k] = v.replace("\r\n", "\n");
	return f.map

def GetSettings(issue):
	f = GetForm("/" + issue + "/edit")
	if not f:
		print "PUB"
		f = GetForm("/" + issue + "/publish")
	return f

def CreateIssue(subject, desc):
	form_fields = [
		("content_upload", "1"),
#		("user", upload_options.email),
		("reviewers", ''),
		("cc", ''),
		("description", desc),
		("base_hashes", ""),
		("subject", subject),
	]
	uploaded_diff_file = [
		("data", "data.diff", emptydiff),
	]
	ctype, body = EncodeMultipartFormData(form_fields, uploaded_diff_file)
	response = MySend("/upload", body, content_type=ctype)
	if response != "":
		print >>sys.stderr, "Error creating issue:\n" + response
		sys.exit(2)

def EditDesc(issue, subject=None, desc=None, reviewers=None, cc=None, closed=None):
	form_fields = GetForm("/" + issue + "/edit")
	if subject is not None:
		form_fields['subject'] = subject
	if desc is not None:
		form_fields['description'] = desc
	if reviewers is not None:
		form_fields['reviewers'] = reviewers
	if cc is not None:
		form_fields['cc'] = cc
	if closed is not None:
		form_fields['closed'] = closed
	ctype, body = EncodeMultipartFormData(form_fields.items(), [])
	response = MySend("/" + issue + "/edit", body, content_type=ctype)
	if response != "":
		print >>sys.stderr, "Error editing description:\n" + "Sent form: \n", form_fields, "\n", response
		sys.exit(2)

def PostMessage(issue, message, reviewers=None, cc=None, send_mail=None, subject=None):
	form_fields = GetForm("/" + issue + "/publish")
	if reviewers is not None:
		form_fields['reviewers'] = reviewers
	if cc is not None:
		form_fields['cc'] = cc
	if send_mail is not None:
		form_fields['send_mail'] = send_mail
	if subject is not None:
		form_fields['subject'] = subject
	form_fields['message'] = message
	form_fields['message_only'] = '1'
	ctype, body = EncodeMultipartFormData(form_fields.items(), [])
	response = MySend("/" + issue + "/publish", body, content_type=ctype)
	if response != "":
		print response
		sys.exit(2)

class opt(object):
	pass

def RietveldSetup(ui):
	global upload_options, rpc, server, server_url_base

	# TODO(rsc): If the repository config has no codereview section,
	# do not enable the extension.  This allows users to
	# put the extension in their global .hgrc but only
	# enable it for some repositories.
	# if not ui.has_section("codereview"):
	# 	cmdtable = {}
	# 	return

	# Config options.
	x = ui.config("codereview", "server")
	if x is not None:
		server = x
	
	# TODO(rsc): Take from ui.username?
	email = None
	x = ui.config("codereview", "email")
	if x is not None:
		email = x

	cc = None
	x = ui.config("codereview", "cc")
	if x is not None:
		cc = x
	
	server_url_base = "http://" + server + "/"
	x = ui.config("codereview", "server_url_base")
	if x is not None:
		server_url_base = x
	if not server_url_base.endswith("/"):
		server_url_base += "/"

	testing = ui.config("codereview", "testing")

	upload_options = opt()
	upload_options.email = email
	upload_options.host = None
	upload_options.verbose = 0
	upload_options.description = None
	upload_options.description_file = None
	upload_options.reviewers = None
	upload_options.cc = cc
	upload_options.message = None
	upload_options.issue = None
	upload_options.download_base = False
	upload_options.revision = None
	upload_options.send_mail = False
	upload_options.vcs = None
	upload_options.server = server
	upload_options.save_cookies = True
	
	if testing:
		upload_options.save_cookies = False
		upload_options.email = "test@example.com"

	rpc = None

#######################################################################
# We keep a full copy of upload.py here to avoid import path hell.
# It would be nice if hg added the hg repository root
# to the default PYTHONPATH.

# Edit .+2,<hget http://codereview.appspot.com/static/upload.py

#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool for uploading diffs from a version control system to the codereview app.

Usage summary: upload.py [options] [-- diff_options]

Diff options are passed to the diff command of the underlying system.

Supported version control systems:
  Git
  Mercurial
  Subversion

It is important for Git/Mercurial users to specify a tree/node/branch to diff
against by using the '--rev' option.
"""
# This code is derived from appcfg.py in the App Engine SDK (open source),
# and from ASPN recipe #146306.

import cookielib
import getpass
import logging
import mimetypes
import optparse
import os
import re
import socket
import subprocess
import sys
import urllib
import urllib2
import urlparse

# The md5 module was deprecated in Python 2.5.
try:
  from hashlib import md5
except ImportError:
  from md5 import md5

try:
  import readline
except ImportError:
  pass

# The logging verbosity:
#  0: Errors only.
#  1: Status messages.
#  2: Info logs.
#  3: Debug logs.
verbosity = 1

# Max size of patch or base file.
MAX_UPLOAD_SIZE = 900 * 1024

# Constants for version control names.  Used by GuessVCSName.
VCS_GIT = "Git"
VCS_MERCURIAL = "Mercurial"
VCS_SUBVERSION = "Subversion"
VCS_UNKNOWN = "Unknown"

# whitelist for non-binary filetypes which do not start with "text/"
# .mm (Objective-C) shows up as application/x-freemind on my Linux box.
TEXT_MIMETYPES = ['application/javascript', 'application/x-javascript',
                  'application/x-freemind']

VCS_ABBREVIATIONS = {
  VCS_MERCURIAL.lower(): VCS_MERCURIAL,
  "hg": VCS_MERCURIAL,
  VCS_SUBVERSION.lower(): VCS_SUBVERSION,
  "svn": VCS_SUBVERSION,
  VCS_GIT.lower(): VCS_GIT,
}


def GetEmail(prompt):
  """Prompts the user for their email address and returns it.

  The last used email address is saved to a file and offered up as a suggestion
  to the user. If the user presses enter without typing in anything the last
  used email address is used. If the user enters a new address, it is saved
  for next time we prompt.

  """
  last_email_file_name = os.path.expanduser("~/.last_codereview_email_address")
  last_email = ""
  if os.path.exists(last_email_file_name):
    try:
      last_email_file = open(last_email_file_name, "r")
      last_email = last_email_file.readline().strip("\n")
      last_email_file.close()
      prompt += " [%s]" % last_email
    except IOError, e:
      pass
  email = raw_input(prompt + ": ").strip()
  if email:
    try:
      last_email_file = open(last_email_file_name, "w")
      last_email_file.write(email)
      last_email_file.close()
    except IOError, e:
      pass
  else:
    email = last_email
  return email


def StatusUpdate(msg):
  """Print a status message to stdout.

  If 'verbosity' is greater than 0, print the message.

  Args:
    msg: The string to print.
  """
  if verbosity > 0:
    print msg


def ErrorExit(msg):
  """Print an error message to stderr and exit."""
  print >>sys.stderr, msg
  sys.exit(1)


class ClientLoginError(urllib2.HTTPError):
  """Raised to indicate there was an error authenticating with ClientLogin."""

  def __init__(self, url, code, msg, headers, args):
    urllib2.HTTPError.__init__(self, url, code, msg, headers, None)
    self.args = args
    self.reason = args["Error"]


class AbstractRpcServer(object):
  """Provides a common interface for a simple RPC server."""

  def __init__(self, host, auth_function, host_override=None, extra_headers={},
               save_cookies=False):
    """Creates a new HttpRpcServer.

    Args:
      host: The host to send requests to.
      auth_function: A function that takes no arguments and returns an
        (email, password) tuple when called. Will be called if authentication
        is required.
      host_override: The host header to send to the server (defaults to host).
      extra_headers: A dict of extra headers to append to every request.
      save_cookies: If True, save the authentication cookies to local disk.
        If False, use an in-memory cookiejar instead.  Subclasses must
        implement this functionality.  Defaults to False.
    """
    self.host = host
    self.host_override = host_override
    self.auth_function = auth_function
    self.authenticated = False
    self.extra_headers = extra_headers
    self.save_cookies = save_cookies
    self.opener = self._GetOpener()
    if self.host_override:
      logging.info("Server: %s; Host: %s", self.host, self.host_override)
    else:
      logging.info("Server: %s", self.host)

  def _GetOpener(self):
    """Returns an OpenerDirector for making HTTP requests.

    Returns:
      A urllib2.OpenerDirector object.
    """
    raise NotImplementedError()

  def _CreateRequest(self, url, data=None):
    """Creates a new urllib request."""
    logging.debug("Creating request for: '%s' with payload:\n%s", url, data)
    req = urllib2.Request(url, data=data)
    if self.host_override:
      req.add_header("Host", self.host_override)
    for key, value in self.extra_headers.iteritems():
      req.add_header(key, value)
    return req

  def _GetAuthToken(self, email, password):
    """Uses ClientLogin to authenticate the user, returning an auth token.

    Args:
      email:    The user's email address
      password: The user's password

    Raises:
      ClientLoginError: If there was an error authenticating with ClientLogin.
      HTTPError: If there was some other form of HTTP error.

    Returns:
      The authentication token returned by ClientLogin.
    """
    account_type = "GOOGLE"
    if self.host.endswith(".google.com"):
      # Needed for use inside Google.
      account_type = "HOSTED"
    req = self._CreateRequest(
        url="https://www.google.com/accounts/ClientLogin",
        data=urllib.urlencode({
            "Email": email,
            "Passwd": password,
            "service": "ah",
            "source": "rietveld-codereview-upload",
            "accountType": account_type,
        }),
    )
    try:
      response = self.opener.open(req)
      response_body = response.read()
      response_dict = dict(x.split("=")
                           for x in response_body.split("\n") if x)
      return response_dict["Auth"]
    except urllib2.HTTPError, e:
      if e.code == 403:
        body = e.read()
        response_dict = dict(x.split("=", 1) for x in body.split("\n") if x)
        raise ClientLoginError(req.get_full_url(), e.code, e.msg,
                               e.headers, response_dict)
      else:
        raise

  def _GetAuthCookie(self, auth_token):
    """Fetches authentication cookies for an authentication token.

    Args:
      auth_token: The authentication token returned by ClientLogin.

    Raises:
      HTTPError: If there was an error fetching the authentication cookies.
    """
    # This is a dummy value to allow us to identify when we're successful.
    continue_location = "http://localhost/"
    args = {"continue": continue_location, "auth": auth_token}
    req = self._CreateRequest("http://%s/_ah/login?%s" %
                              (self.host, urllib.urlencode(args)))
    try:
      response = self.opener.open(req)
    except urllib2.HTTPError, e:
      response = e
    if (response.code != 302 or
        response.info()["location"] != continue_location):
      raise urllib2.HTTPError(req.get_full_url(), response.code, response.msg,
                              response.headers, response.fp)
    self.authenticated = True

  def _Authenticate(self):
    """Authenticates the user.

    The authentication process works as follows:
     1) We get a username and password from the user
     2) We use ClientLogin to obtain an AUTH token for the user
        (see http://code.google.com/apis/accounts/AuthForInstalledApps.html).
     3) We pass the auth token to /_ah/login on the server to obtain an
        authentication cookie. If login was successful, it tries to redirect
        us to the URL we provided.

    If we attempt to access the upload API without first obtaining an
    authentication cookie, it returns a 401 response (or a 302) and
    directs us to authenticate ourselves with ClientLogin.
    """
    for i in range(3):
      credentials = self.auth_function()
      try:
        auth_token = self._GetAuthToken(credentials[0], credentials[1])
      except ClientLoginError, e:
        if e.reason == "BadAuthentication":
          print >>sys.stderr, "Invalid username or password."
          continue
        if e.reason == "CaptchaRequired":
          print >>sys.stderr, (
              "Please go to\n"
              "https://www.google.com/accounts/DisplayUnlockCaptcha\n"
              "and verify you are a human.  Then try again.")
          break
        if e.reason == "NotVerified":
          print >>sys.stderr, "Account not verified."
          break
        if e.reason == "TermsNotAgreed":
          print >>sys.stderr, "User has not agreed to TOS."
          break
        if e.reason == "AccountDeleted":
          print >>sys.stderr, "The user account has been deleted."
          break
        if e.reason == "AccountDisabled":
          print >>sys.stderr, "The user account has been disabled."
          break
        if e.reason == "ServiceDisabled":
          print >>sys.stderr, ("The user's access to the service has been "
                               "disabled.")
          break
        if e.reason == "ServiceUnavailable":
          print >>sys.stderr, "The service is not available; try again later."
          break
        raise
      self._GetAuthCookie(auth_token)
      return

  def Send(self, request_path, payload=None,
           content_type="application/octet-stream",
           timeout=None,
           **kwargs):
    """Sends an RPC and returns the response.

    Args:
      request_path: The path to send the request to, eg /api/appversion/create.
      payload: The body of the request, or None to send an empty request.
      content_type: The Content-Type header to use.
      timeout: timeout in seconds; default None i.e. no timeout.
        (Note: for large requests on OS X, the timeout doesn't work right.)
      kwargs: Any keyword arguments are converted into query string parameters.

    Returns:
      The response body, as a string.
    """
    # TODO: Don't require authentication.  Let the server say
    # whether it is necessary.
    if not self.authenticated:
      self._Authenticate()

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
      tries = 0
      while True:
        tries += 1
        args = dict(kwargs)
        url = "http://%s%s" % (self.host, request_path)
        if args:
          url += "?" + urllib.urlencode(args)
        req = self._CreateRequest(url=url, data=payload)
        req.add_header("Content-Type", content_type)
        try:
          f = self.opener.open(req)
          response = f.read()
          f.close()
          return response
        except urllib2.HTTPError, e:
          if tries > 3:
            raise
          elif e.code == 401 or e.code == 302:
            self._Authenticate()
##           elif e.code >= 500 and e.code < 600:
##             # Server Error - try again.
##             continue
          else:
            raise
    finally:
      socket.setdefaulttimeout(old_timeout)


class HttpRpcServer(AbstractRpcServer):
  """Provides a simplified RPC-style interface for HTTP requests."""

  def _Authenticate(self):
    """Save the cookie jar after authentication."""
    super(HttpRpcServer, self)._Authenticate()
    if self.save_cookies:
      StatusUpdate("Saving authentication cookies to %s" % self.cookie_file)
      self.cookie_jar.save()

  def _GetOpener(self):
    """Returns an OpenerDirector that supports cookies and ignores redirects.

    Returns:
      A urllib2.OpenerDirector object.
    """
    opener = urllib2.OpenerDirector()
    opener.add_handler(urllib2.ProxyHandler())
    opener.add_handler(urllib2.UnknownHandler())
    opener.add_handler(urllib2.HTTPHandler())
    opener.add_handler(urllib2.HTTPDefaultErrorHandler())
    opener.add_handler(urllib2.HTTPSHandler())
    opener.add_handler(urllib2.HTTPErrorProcessor())
    if self.save_cookies:
      self.cookie_file = os.path.expanduser("~/.codereview_upload_cookies_" + server)
      self.cookie_jar = cookielib.MozillaCookieJar(self.cookie_file)
      if os.path.exists(self.cookie_file):
        try:
          self.cookie_jar.load()
          self.authenticated = True
          StatusUpdate("Loaded authentication cookies from %s" %
                       self.cookie_file)
        except (cookielib.LoadError, IOError):
          # Failed to load cookies - just ignore them.
          pass
      else:
        # Create an empty cookie file with mode 600
        fd = os.open(self.cookie_file, os.O_CREAT, 0600)
        os.close(fd)
      # Always chmod the cookie file
      os.chmod(self.cookie_file, 0600)
    else:
      # Don't save cookies across runs of update.py.
      self.cookie_jar = cookielib.CookieJar()
    opener.add_handler(urllib2.HTTPCookieProcessor(self.cookie_jar))
    return opener


parser = optparse.OptionParser(usage="%prog [options] [-- diff_options]")
parser.add_option("-y", "--assume_yes", action="store_true",
                  dest="assume_yes", default=False,
                  help="Assume that the answer to yes/no questions is 'yes'.")
# Logging
group = parser.add_option_group("Logging options")
group.add_option("-q", "--quiet", action="store_const", const=0,
                 dest="verbose", help="Print errors only.")
group.add_option("-v", "--verbose", action="store_const", const=2,
                 dest="verbose", default=1,
                 help="Print info level logs (default).")
group.add_option("--noisy", action="store_const", const=3,
                 dest="verbose", help="Print all logs.")
# Review server
group = parser.add_option_group("Review server options")
group.add_option("-s", "--server", action="store", dest="server",
                 default="codereview.appspot.com",
                 metavar="SERVER",
                 help=("The server to upload to. The format is host[:port]. "
                       "Defaults to '%default'."))
group.add_option("-e", "--email", action="store", dest="email",
                 metavar="EMAIL", default=None,
                 help="The username to use. Will prompt if omitted.")
group.add_option("-H", "--host", action="store", dest="host",
                 metavar="HOST", default=None,
                 help="Overrides the Host header sent with all RPCs.")
group.add_option("--no_cookies", action="store_false",
                 dest="save_cookies", default=True,
                 help="Do not save authentication cookies to local disk.")
# Issue
group = parser.add_option_group("Issue options")
group.add_option("-d", "--description", action="store", dest="description",
                 metavar="DESCRIPTION", default=None,
                 help="Optional description when creating an issue.")
group.add_option("-f", "--description_file", action="store",
                 dest="description_file", metavar="DESCRIPTION_FILE",
                 default=None,
                 help="Optional path of a file that contains "
                      "the description when creating an issue.")
group.add_option("-r", "--reviewers", action="store", dest="reviewers",
                 metavar="REVIEWERS", default=None,
                 help="Add reviewers (comma separated email addresses).")
group.add_option("--cc", action="store", dest="cc",
                 metavar="CC", default=None,
                 help="Add CC (comma separated email addresses).")
group.add_option("--private", action="store_true", dest="private",
                 default=False,
                 help="Make the issue restricted to reviewers and those CCed")
# Upload options
group = parser.add_option_group("Patch options")
group.add_option("-m", "--message", action="store", dest="message",
                 metavar="MESSAGE", default=None,
                 help="A message to identify the patch. "
                      "Will prompt if omitted.")
group.add_option("-i", "--issue", type="int", action="store",
                 metavar="ISSUE", default=None,
                 help="Issue number to which to add. Defaults to new issue.")
group.add_option("--download_base", action="store_true",
                 dest="download_base", default=False,
                 help="Base files will be downloaded by the server "
                 "(side-by-side diffs may not work on files with CRs).")
group.add_option("--rev", action="store", dest="revision",
                 metavar="REV", default=None,
                 help="Branch/tree/revision to diff against (used by DVCS).")
group.add_option("--send_mail", action="store_true",
                 dest="send_mail", default=False,
                 help="Send notification email to reviewers.")
group.add_option("--vcs", action="store", dest="vcs",
                 metavar="VCS", default=None,
                 help=("Version control system (optional, usually upload.py "
                       "already guesses the right VCS)."))


def GetRpcServer(options):
  """Returns an instance of an AbstractRpcServer.

  Returns:
    A new AbstractRpcServer, on which RPC calls can be made.
  """

  rpc_server_class = HttpRpcServer

  def GetUserCredentials():
    """Prompts the user for a username and password."""
    email = options.email
    if email is None:
      email = GetEmail("Email (login for uploading to %s)" % options.server)
    password = getpass.getpass("Password for %s: " % email)
    return (email, password)

  # If this is the dev_appserver, use fake authentication.
  host = (options.host or options.server).lower()
  if host == "localhost" or host.startswith("localhost:"):
    email = options.email
    if email is None:
      email = "test@example.com"
      logging.info("Using debug user %s.  Override with --email" % email)
    server = rpc_server_class(
        options.server,
        lambda: (email, "password"),
        host_override=options.host,
        extra_headers={"Cookie":
                       'dev_appserver_login="%s:False"' % email},
        save_cookies=options.save_cookies)
    # Don't try to talk to ClientLogin.
    server.authenticated = True
    return server

  return rpc_server_class(options.server, GetUserCredentials,
                          host_override=options.host,
                          save_cookies=options.save_cookies)


def EncodeMultipartFormData(fields, files):
  """Encode form fields for multipart/form-data.

  Args:
    fields: A sequence of (name, value) elements for regular form fields.
    files: A sequence of (name, filename, value) elements for data to be
           uploaded as files.
  Returns:
    (content_type, body) ready for httplib.HTTP instance.

  Source:
    http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/146306
  """
  BOUNDARY = '-M-A-G-I-C---B-O-U-N-D-A-R-Y-'
  CRLF = '\r\n'
  lines = []
  for (key, value) in fields:
    lines.append('--' + BOUNDARY)
    lines.append('Content-Disposition: form-data; name="%s"' % key)
    lines.append('')
    lines.append(value)
  for (key, filename, value) in files:
    lines.append('--' + BOUNDARY)
    lines.append('Content-Disposition: form-data; name="%s"; filename="%s"' %
             (key, filename))
    lines.append('Content-Type: %s' % GetContentType(filename))
    lines.append('')
    lines.append(value)
  lines.append('--' + BOUNDARY + '--')
  lines.append('')
  body = CRLF.join(lines)
  content_type = 'multipart/form-data; boundary=%s' % BOUNDARY
  return content_type, body


def GetContentType(filename):
  """Helper to guess the content-type from the filename."""
  return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


# Use a shell for subcommands on Windows to get a PATH search.
use_shell = sys.platform.startswith("win")

def RunShellWithReturnCode(command, print_output=False,
                           universal_newlines=True,
                           env=os.environ):
  """Executes a command and returns the output from stdout and the return code.

  Args:
    command: Command to execute.
    print_output: If True, the output is printed to stdout.
                  If False, both stdout and stderr are ignored.
    universal_newlines: Use universal_newlines flag (default: True).

  Returns:
    Tuple (output, return code)
  """
  logging.info("Running %s", command)
  p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       shell=use_shell, universal_newlines=universal_newlines,
                       env=env)
  if print_output:
    output_array = []
    while True:
      line = p.stdout.readline()
      if not line:
        break
      print line.strip("\n")
      output_array.append(line)
    output = "".join(output_array)
  else:
    output = p.stdout.read()
  p.wait()
  errout = p.stderr.read()
  if print_output and errout:
    print >>sys.stderr, errout
  p.stdout.close()
  p.stderr.close()
  return output, p.returncode


def RunShell(command, silent_ok=False, universal_newlines=True,
             print_output=False, env=os.environ):
  data, retcode = RunShellWithReturnCode(command, print_output,
                                         universal_newlines, env)
  if retcode:
    ErrorExit("Got error status from %s:\n%s" % (command, data))
  if not silent_ok and not data:
    ErrorExit("No output from %s" % command)
  return data


class VersionControlSystem(object):
  """Abstract base class providing an interface to the VCS."""

  def __init__(self, options):
    """Constructor.

    Args:
      options: Command line options.
    """
    self.options = options

  def GenerateDiff(self, args):
    """Return the current diff as a string.

    Args:
      args: Extra arguments to pass to the diff command.
    """
    raise NotImplementedError(
        "abstract method -- subclass %s must override" % self.__class__)

  def GetUnknownFiles(self):
    """Return a list of files unknown to the VCS."""
    raise NotImplementedError(
        "abstract method -- subclass %s must override" % self.__class__)

  def CheckForUnknownFiles(self):
    """Show an "are you sure?" prompt if there are unknown files."""
    unknown_files = self.GetUnknownFiles()
    if unknown_files:
      print "The following files are not added to version control:"
      for line in unknown_files:
        print line
      prompt = "Are you sure to continue?(y/N) "
      answer = raw_input(prompt).strip()
      if answer != "y":
        ErrorExit("User aborted")

  def GetBaseFile(self, filename):
    """Get the content of the upstream version of a file.

    Returns:
      A tuple (base_content, new_content, is_binary, status)
        base_content: The contents of the base file.
        new_content: For text files, this is empty.  For binary files, this is
          the contents of the new file, since the diff output won't contain
          information to reconstruct the current file.
        is_binary: True iff the file is binary.
        status: The status of the file.
    """

    raise NotImplementedError(
        "abstract method -- subclass %s must override" % self.__class__)


  def GetBaseFiles(self, diff):
    """Helper that calls GetBase file for each file in the patch.

    Returns:
      A dictionary that maps from filename to GetBaseFile's tuple.  Filenames
      are retrieved based on lines that start with "Index:" or
      "Property changes on:".
    """
    files = {}
    for line in diff.splitlines(True):
      if line.startswith('Index:') or line.startswith('Property changes on:'):
        unused, filename = line.split(':', 1)
        # On Windows if a file has property changes its filename uses '\'
        # instead of '/'.
        filename = filename.strip().replace('\\', '/')
        files[filename] = self.GetBaseFile(filename)
    return files


  def UploadBaseFiles(self, issue, rpc_server, patch_list, patchset, options,
                      files):
    """Uploads the base files (and if necessary, the current ones as well)."""

    def UploadFile(filename, file_id, content, is_binary, status, is_base):
      """Uploads a file to the server."""
      file_too_large = False
      if is_base:
        type = "base"
      else:
        type = "current"
      if len(content) > MAX_UPLOAD_SIZE:
        print ("Not uploading the %s file for %s because it's too large." %
               (type, filename))
        file_too_large = True
        content = ""
      checksum = md5(content).hexdigest()
      if options.verbose > 0 and not file_too_large:
        print "Uploading %s file for %s" % (type, filename)
      url = "/%d/upload_content/%d/%d" % (int(issue), int(patchset), file_id)
      form_fields = [("filename", filename),
                     ("status", status),
                     ("checksum", checksum),
                     ("is_binary", str(is_binary)),
                     ("is_current", str(not is_base)),
                    ]
      if file_too_large:
        form_fields.append(("file_too_large", "1"))
      if options.email:
        form_fields.append(("user", options.email))
      ctype, body = EncodeMultipartFormData(form_fields,
                                            [("data", filename, content)])
      response_body = rpc_server.Send(url, body,
                                      content_type=ctype)
      if not response_body.startswith("OK"):
        StatusUpdate("  --> %s" % response_body)
        sys.exit(1)

    patches = dict()
    [patches.setdefault(v, k) for k, v in patch_list]
    for filename in patches.keys():
      base_content, new_content, is_binary, status = files[filename]
      file_id_str = patches.get(filename)
      if file_id_str.find("nobase") != -1:
        base_content = None
        file_id_str = file_id_str[file_id_str.rfind("_") + 1:]
      file_id = int(file_id_str)
      if base_content != None:
        UploadFile(filename, file_id, base_content, is_binary, status, True)
      if new_content != None:
        UploadFile(filename, file_id, new_content, is_binary, status, False)

  def IsImage(self, filename):
    """Returns true if the filename has an image extension."""
    mimetype =  mimetypes.guess_type(filename)[0]
    if not mimetype:
      return False
    return mimetype.startswith("image/")

  def IsBinary(self, filename):
    """Returns true if the guessed mimetyped isnt't in text group."""
    mimetype = mimetypes.guess_type(filename)[0]
    if not mimetype:
      return False  # e.g. README, "real" binaries usually have an extension
    # special case for text files which don't start with text/
    if mimetype in TEXT_MIMETYPES:
      return False
    return not mimetype.startswith("text/")


class SubversionVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Subversion."""

  def __init__(self, options):
    super(SubversionVCS, self).__init__(options)
    if self.options.revision:
      match = re.match(r"(\d+)(:(\d+))?", self.options.revision)
      if not match:
        ErrorExit("Invalid Subversion revision %s." % self.options.revision)
      self.rev_start = match.group(1)
      self.rev_end = match.group(3)
    else:
      self.rev_start = self.rev_end = None
    # Cache output from "svn list -r REVNO dirname".
    # Keys: dirname, Values: 2-tuple (ouput for start rev and end rev).
    self.svnls_cache = {}
    # SVN base URL is required to fetch files deleted in an older revision.
    # Result is cached to not guess it over and over again in GetBaseFile().
    required = self.options.download_base or self.options.revision is not None
    self.svn_base = self._GuessBase(required)

  def GuessBase(self, required):
    """Wrapper for _GuessBase."""
    return self.svn_base

  def _GuessBase(self, required):
    """Returns the SVN base URL.

    Args:
      required: If true, exits if the url can't be guessed, otherwise None is
        returned.
    """
    info = RunShell(["svn", "info"])
    for line in info.splitlines():
      words = line.split()
      if len(words) == 2 and words[0] == "URL:":
        url = words[1]
        scheme, netloc, path, params, query, fragment = urlparse.urlparse(url)
        username, netloc = urllib.splituser(netloc)
        if username:
          logging.info("Removed username from base URL")
        if netloc.endswith("svn.python.org"):
          if netloc == "svn.python.org":
            if path.startswith("/projects/"):
              path = path[9:]
          elif netloc != "pythondev@svn.python.org":
            ErrorExit("Unrecognized Python URL: %s" % url)
          base = "http://svn.python.org/view/*checkout*%s/" % path
          logging.info("Guessed Python base = %s", base)
        elif netloc.endswith("svn.collab.net"):
          if path.startswith("/repos/"):
            path = path[6:]
          base = "http://svn.collab.net/viewvc/*checkout*%s/" % path
          logging.info("Guessed CollabNet base = %s", base)
        elif netloc.endswith(".googlecode.com"):
          path = path + "/"
          base = urlparse.urlunparse(("http", netloc, path, params,
                                      query, fragment))
          logging.info("Guessed Google Code base = %s", base)
        else:
          path = path + "/"
          base = urlparse.urlunparse((scheme, netloc, path, params,
                                      query, fragment))
          logging.info("Guessed base = %s", base)
        return base
    if required:
      ErrorExit("Can't find URL in output from svn info")
    return None

  def GenerateDiff(self, args):
    cmd = ["svn", "diff"]
    if self.options.revision:
      cmd += ["-r", self.options.revision]
    cmd.extend(args)
    data = RunShell(cmd)
    count = 0
    for line in data.splitlines():
      if line.startswith("Index:") or line.startswith("Property changes on:"):
        count += 1
        logging.info(line)
    if not count:
      ErrorExit("No valid patches found in output from svn diff")
    return data

  def _CollapseKeywords(self, content, keyword_str):
    """Collapses SVN keywords."""
    # svn cat translates keywords but svn diff doesn't. As a result of this
    # behavior patching.PatchChunks() fails with a chunk mismatch error.
    # This part was originally written by the Review Board development team
    # who had the same problem (http://reviews.review-board.org/r/276/).
    # Mapping of keywords to known aliases
    svn_keywords = {
      # Standard keywords
      'Date':                ['Date', 'LastChangedDate'],
      'Revision':            ['Revision', 'LastChangedRevision', 'Rev'],
      'Author':              ['Author', 'LastChangedBy'],
      'HeadURL':             ['HeadURL', 'URL'],
      'Id':                  ['Id'],

      # Aliases
      'LastChangedDate':     ['LastChangedDate', 'Date'],
      'LastChangedRevision': ['LastChangedRevision', 'Rev', 'Revision'],
      'LastChangedBy':       ['LastChangedBy', 'Author'],
      'URL':                 ['URL', 'HeadURL'],
    }

    def repl(m):
       if m.group(2):
         return "$%s::%s$" % (m.group(1), " " * len(m.group(3)))
       return "$%s$" % m.group(1)
    keywords = [keyword
                for name in keyword_str.split(" ")
                for keyword in svn_keywords.get(name, [])]
    return re.sub(r"\$(%s):(:?)([^\$]+)\$" % '|'.join(keywords), repl, content)

  def GetUnknownFiles(self):
    status = RunShell(["svn", "status", "--ignore-externals"], silent_ok=True)
    unknown_files = []
    for line in status.split("\n"):
      if line and line[0] == "?":
        unknown_files.append(line)
    return unknown_files

  def ReadFile(self, filename):
    """Returns the contents of a file."""
    file = open(filename, 'rb')
    result = ""
    try:
      result = file.read()
    finally:
      file.close()
    return result

  def GetStatus(self, filename):
    """Returns the status of a file."""
    if not self.options.revision:
      status = RunShell(["svn", "status", "--ignore-externals", filename])
      if not status:
        ErrorExit("svn status returned no output for %s" % filename)
      status_lines = status.splitlines()
      # If file is in a cl, the output will begin with
      # "\n--- Changelist 'cl_name':\n".  See
      # http://svn.collab.net/repos/svn/trunk/notes/changelist-design.txt
      if (len(status_lines) == 3 and
          not status_lines[0] and
          status_lines[1].startswith("--- Changelist")):
        status = status_lines[2]
      else:
        status = status_lines[0]
    # If we have a revision to diff against we need to run "svn list"
    # for the old and the new revision and compare the results to get
    # the correct status for a file.
    else:
      dirname, relfilename = os.path.split(filename)
      if dirname not in self.svnls_cache:
        cmd = ["svn", "list", "-r", self.rev_start, dirname or "."]
        out, returncode = RunShellWithReturnCode(cmd)
        if returncode:
          ErrorExit("Failed to get status for %s." % filename)
        old_files = out.splitlines()
        args = ["svn", "list"]
        if self.rev_end:
          args += ["-r", self.rev_end]
        cmd = args + [dirname or "."]
        out, returncode = RunShellWithReturnCode(cmd)
        if returncode:
          ErrorExit("Failed to run command %s" % cmd)
        self.svnls_cache[dirname] = (old_files, out.splitlines())
      old_files, new_files = self.svnls_cache[dirname]
      if relfilename in old_files and relfilename not in new_files:
        status = "D   "
      elif relfilename in old_files and relfilename in new_files:
        status = "M   "
      else:
        status = "A   "
    return status

  def GetBaseFile(self, filename):
    status = self.GetStatus(filename)
    base_content = None
    new_content = None

    # If a file is copied its status will be "A  +", which signifies
    # "addition-with-history".  See "svn st" for more information.  We need to
    # upload the original file or else diff parsing will fail if the file was
    # edited.
    if status[0] == "A" and status[3] != "+":
      # We'll need to upload the new content if we're adding a binary file
      # since diff's output won't contain it.
      mimetype = RunShell(["svn", "propget", "svn:mime-type", filename],
                          silent_ok=True)
      base_content = ""
      is_binary = bool(mimetype) and not mimetype.startswith("text/")
      if is_binary and self.IsImage(filename):
        new_content = self.ReadFile(filename)
    elif (status[0] in ("M", "D", "R") or
          (status[0] == "A" and status[3] == "+") or  # Copied file.
          (status[0] == " " and status[1] == "M")):  # Property change.
      args = []
      if self.options.revision:
        url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
      else:
        # Don't change filename, it's needed later.
        url = filename
        args += ["-r", "BASE"]
      cmd = ["svn"] + args + ["propget", "svn:mime-type", url]
      mimetype, returncode = RunShellWithReturnCode(cmd)
      if returncode:
        # File does not exist in the requested revision.
        # Reset mimetype, it contains an error message.
        mimetype = ""
      get_base = False
      is_binary = bool(mimetype) and not mimetype.startswith("text/")
      if status[0] == " ":
        # Empty base content just to force an upload.
        base_content = ""
      elif is_binary:
        if self.IsImage(filename):
          get_base = True
          if status[0] == "M":
            if not self.rev_end:
              new_content = self.ReadFile(filename)
            else:
              url = "%s/%s@%s" % (self.svn_base, filename, self.rev_end)
              new_content = RunShell(["svn", "cat", url],
                                     universal_newlines=True, silent_ok=True)
        else:
          base_content = ""
      else:
        get_base = True

      if get_base:
        if is_binary:
          universal_newlines = False
        else:
          universal_newlines = True
        if self.rev_start:
          # "svn cat -r REV delete_file.txt" doesn't work. cat requires
          # the full URL with "@REV" appended instead of using "-r" option.
          url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
          base_content = RunShell(["svn", "cat", url],
                                  universal_newlines=universal_newlines,
                                  silent_ok=True)
        else:
          base_content = RunShell(["svn", "cat", filename],
                                  universal_newlines=universal_newlines,
                                  silent_ok=True)
        if not is_binary:
          args = []
          if self.rev_start:
            url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
          else:
            url = filename
            args += ["-r", "BASE"]
          cmd = ["svn"] + args + ["propget", "svn:keywords", url]
          keywords, returncode = RunShellWithReturnCode(cmd)
          if keywords and not returncode:
            base_content = self._CollapseKeywords(base_content, keywords)
    else:
      StatusUpdate("svn status returned unexpected output: %s" % status)
      sys.exit(1)
    return base_content, new_content, is_binary, status[0:5]


class GitVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Git."""

  def __init__(self, options):
    super(GitVCS, self).__init__(options)
    # Map of filename -> (hash before, hash after) of base file.
    # Hashes for "no such file" are represented as None.
    self.hashes = {}
    # Map of new filename -> old filename for renames.
    self.renames = {}

  def GenerateDiff(self, extra_args):
    # This is more complicated than svn's GenerateDiff because we must convert
    # the diff output to include an svn-style "Index:" line as well as record
    # the hashes of the files, so we can upload them along with our diff.

    # Special used by git to indicate "no such content".
    NULL_HASH = "0"*40

    extra_args = extra_args[:]
    if self.options.revision:
      extra_args = [self.options.revision] + extra_args
    extra_args.append('-M')

    # --no-ext-diff is broken in some versions of Git, so try to work around
    # this by overriding the environment (but there is still a problem if the
    # git config key "diff.external" is used).
    env = os.environ.copy()
    if 'GIT_EXTERNAL_DIFF' in env: del env['GIT_EXTERNAL_DIFF']
    gitdiff = RunShell(["git", "diff", "--no-ext-diff", "--full-index"]
                       + extra_args, env=env)
    svndiff = []
    filecount = 0
    filename = None
    for line in gitdiff.splitlines():
      match = re.match(r"diff --git a/(.*) b/(.*)$", line)
      if match:
        filecount += 1
        # Intentionally use the "after" filename so we can show renames.
        filename = match.group(2)
        svndiff.append("Index: %s\n" % filename)
        if match.group(1) != match.group(2):
          self.renames[match.group(2)] = match.group(1)
      else:
        # The "index" line in a git diff looks like this (long hashes elided):
        #   index 82c0d44..b2cee3f 100755
        # We want to save the left hash, as that identifies the base file.
        match = re.match(r"index (\w+)\.\.(\w+)", line)
        if match:
          before, after = (match.group(1), match.group(2))
          if before == NULL_HASH:
            before = None
          if after == NULL_HASH:
            after = None
          self.hashes[filename] = (before, after)
      svndiff.append(line + "\n")
    if not filecount:
      ErrorExit("No valid patches found in output from git diff")
    return "".join(svndiff)

  def GetUnknownFiles(self):
    status = RunShell(["git", "ls-files", "--exclude-standard", "--others"],
                      silent_ok=True)
    return status.splitlines()

  def GetFileContent(self, file_hash, is_binary):
    """Returns the content of a file identified by its git hash."""
    data, retcode = RunShellWithReturnCode(["git", "show", file_hash],
                                            universal_newlines=not is_binary)
    if retcode:
      ErrorExit("Got error status from 'git show %s'" % file_hash)
    return data

  def GetBaseFile(self, filename):
    hash_before, hash_after = self.hashes.get(filename, (None,None))
    base_content = None
    new_content = None
    is_binary = self.IsBinary(filename)
    status = None

    if filename in self.renames:
      status = "A +"  # Match svn attribute name for renames.
      if filename not in self.hashes:
        # If a rename doesn't change the content, we never get a hash.
        base_content = RunShell(["git", "show", filename])
    elif not hash_before:
      status = "A"
      base_content = ""
    elif not hash_after:
      status = "D"
    else:
      status = "M"

    is_image = self.IsImage(filename)

    # Grab the before/after content if we need it.
    # We should include file contents if it's text or it's an image.
    if not is_binary or is_image:
      # Grab the base content if we don't have it already.
      if base_content is None and hash_before:
        base_content = self.GetFileContent(hash_before, is_binary)
      # Only include the "after" file if it's an image; otherwise it
      # it is reconstructed from the diff.
      if is_image and hash_after:
        new_content = self.GetFileContent(hash_after, is_binary)

    return (base_content, new_content, is_binary, status)


class MercurialVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Mercurial."""

  def __init__(self, options, repo_dir):
    super(MercurialVCS, self).__init__(options)
    # Absolute path to repository (we can be in a subdir)
    self.repo_dir = os.path.normpath(repo_dir)
    # Compute the subdir
    cwd = os.path.normpath(os.getcwd())
    assert cwd.startswith(self.repo_dir)
    self.subdir = cwd[len(self.repo_dir):].lstrip(r"\/")
    if self.options.revision:
      self.base_rev = self.options.revision
    else:
      self.base_rev = RunShell(["hg", "parent", "-q"]).split(':')[1].strip()

  def _GetRelPath(self, filename):
    """Get relative path of a file according to the current directory,
    given its logical path in the repo."""
    assert filename.startswith(self.subdir), (filename, self.subdir)
    return filename[len(self.subdir):].lstrip(r"\/")

  def GenerateDiff(self, extra_args):
    # If no file specified, restrict to the current subdir
    extra_args = extra_args or ["."]
    cmd = ["hg", "diff", "--git", "-r", self.base_rev] + extra_args
    data = RunShell(cmd, silent_ok=True)
    svndiff = []
    filecount = 0
    for line in data.splitlines():
      m = re.match("diff --git a/(\S+) b/(\S+)", line)
      if m:
        # Modify line to make it look like as it comes from svn diff.
        # With this modification no changes on the server side are required
        # to make upload.py work with Mercurial repos.
        # NOTE: for proper handling of moved/copied files, we have to use
        # the second filename.
        filename = m.group(2)
        svndiff.append("Index: %s" % filename)
        svndiff.append("=" * 67)
        filecount += 1
        logging.info(line)
      else:
        svndiff.append(line)
    if not filecount:
      ErrorExit("No valid patches found in output from hg diff")
    return "\n".join(svndiff) + "\n"

  def GetUnknownFiles(self):
    """Return a list of files unknown to the VCS."""
    args = []
    status = RunShell(["hg", "status", "--rev", self.base_rev, "-u", "."],
        silent_ok=True)
    unknown_files = []
    for line in status.splitlines():
      st, fn = line.split(" ", 1)
      if st == "?":
        unknown_files.append(fn)
    return unknown_files

  def GetBaseFile(self, filename):
    # "hg status" and "hg cat" both take a path relative to the current subdir
    # rather than to the repo root, but "hg diff" has given us the full path
    # to the repo root.
    base_content = ""
    new_content = None
    is_binary = False
    oldrelpath = relpath = self._GetRelPath(filename)
    # "hg status -C" returns two lines for moved/copied files, one otherwise
    out = RunShell(["hg", "status", "-C", "--rev", self.base_rev, relpath])
    out = out.splitlines()
    # HACK: strip error message about missing file/directory if it isn't in
    # the working copy
    if out[0].startswith('%s: ' % relpath):
      out = out[1:]
    if len(out) > 1:
      # Moved/copied => considered as modified, use old filename to
      # retrieve base contents
      oldrelpath = out[1].strip()
      status = "M"
    else:
      status, _ = out[0].split(' ', 1)
    if ":" in self.base_rev:
      base_rev = self.base_rev.split(":", 1)[0]
    else:
      base_rev = self.base_rev
    if status != "A":
      base_content = RunShell(["hg", "cat", "-r", base_rev, oldrelpath],
        silent_ok=True)
      is_binary = "\0" in base_content  # Mercurial's heuristic
    if status != "R":
      new_content = open(relpath, "rb").read()
      is_binary = is_binary or "\0" in new_content
    if is_binary and base_content:
      # Fetch again without converting newlines
      base_content = RunShell(["hg", "cat", "-r", base_rev, oldrelpath],
        silent_ok=True, universal_newlines=False)
    if not is_binary or not self.IsImage(relpath):
      new_content = None
    return base_content, new_content, is_binary, status


# NOTE: The SplitPatch function is duplicated in engine.py, keep them in sync.
def SplitPatch(data):
  """Splits a patch into separate pieces for each file.

  Args:
    data: A string containing the output of svn diff.

  Returns:
    A list of 2-tuple (filename, text) where text is the svn diff output
      pertaining to filename.
  """
  patches = []
  filename = None
  diff = []
  for line in data.splitlines(True):
    new_filename = None
    if line.startswith('Index:'):
      unused, new_filename = line.split(':', 1)
      new_filename = new_filename.strip()
    elif line.startswith('Property changes on:'):
      unused, temp_filename = line.split(':', 1)
      # When a file is modified, paths use '/' between directories, however
      # when a property is modified '\' is used on Windows.  Make them the same
      # otherwise the file shows up twice.
      temp_filename = temp_filename.strip().replace('\\', '/')
      if temp_filename != filename:
        # File has property changes but no modifications, create a new diff.
        new_filename = temp_filename
    if new_filename:
      if filename and diff:
        patches.append((filename, ''.join(diff)))
      filename = new_filename
      diff = [line]
      continue
    if diff is not None:
      diff.append(line)
  if filename and diff:
    patches.append((filename, ''.join(diff)))
  return patches


def UploadSeparatePatches(issue, rpc_server, patchset, data, options):
  """Uploads a separate patch for each file in the diff output.

  Returns a list of [patch_key, filename] for each file.
  """
  patches = SplitPatch(data)
  rv = []
  for patch in patches:
    if len(patch[1]) > MAX_UPLOAD_SIZE:
      print ("Not uploading the patch for " + patch[0] +
             " because the file is too large.")
      continue
    form_fields = [("filename", patch[0])]
    if not options.download_base:
      form_fields.append(("content_upload", "1"))
    files = [("data", "data.diff", patch[1])]
    ctype, body = EncodeMultipartFormData(form_fields, files)
    url = "/%d/upload_patch/%d" % (int(issue), int(patchset))
    print "Uploading patch for " + patch[0]
    response_body = rpc_server.Send(url, body, content_type=ctype)
    lines = response_body.splitlines()
    if not lines or lines[0] != "OK":
      StatusUpdate("  --> %s" % response_body)
      sys.exit(1)
    rv.append([lines[1], patch[0]])
  return rv


def GuessVCSName():
  """Helper to guess the version control system.

  This examines the current directory, guesses which VersionControlSystem
  we're using, and returns an string indicating which VCS is detected.

  Returns:
    A pair (vcs, output).  vcs is a string indicating which VCS was detected
    and is one of VCS_GIT, VCS_MERCURIAL, VCS_SUBVERSION, or VCS_UNKNOWN.
    output is a string containing any interesting output from the vcs
    detection routine, or None if there is nothing interesting.
  """
  # Mercurial has a command to get the base directory of a repository
  # Try running it, but don't die if we don't have hg installed.
  # NOTE: we try Mercurial first as it can sit on top of an SVN working copy.
  try:
    out, returncode = RunShellWithReturnCode(["hg", "root"])
    if returncode == 0:
      return (VCS_MERCURIAL, out.strip())
  except OSError, (errno, message):
    if errno != 2:  # ENOENT -- they don't have hg installed.
      raise

  # Subversion has a .svn in all working directories.
  if os.path.isdir('.svn'):
    logging.info("Guessed VCS = Subversion")
    return (VCS_SUBVERSION, None)

  # Git has a command to test if you're in a git tree.
  # Try running it, but don't die if we don't have git installed.
  try:
    out, returncode = RunShellWithReturnCode(["git", "rev-parse",
                                              "--is-inside-work-tree"])
    if returncode == 0:
      return (VCS_GIT, None)
  except OSError, (errno, message):
    if errno != 2:  # ENOENT -- they don't have git installed.
      raise

  return (VCS_UNKNOWN, None)


def GuessVCS(options):
  """Helper to guess the version control system.

  This verifies any user-specified VersionControlSystem (by command line
  or environment variable).  If the user didn't specify one, this examines
  the current directory, guesses which VersionControlSystem we're using,
  and returns an instance of the appropriate class.  Exit with an error
  if we can't figure it out.

  Returns:
    A VersionControlSystem instance. Exits if the VCS can't be guessed.
  """
  vcs = options.vcs
  if not vcs:
    vcs = os.environ.get("CODEREVIEW_VCS")
  if vcs:
    v = VCS_ABBREVIATIONS.get(vcs.lower())
    if v is None:
      ErrorExit("Unknown version control system %r specified." % vcs)
    (vcs, extra_output) = (v, None)
  else:
    (vcs, extra_output) = GuessVCSName()

  if vcs == VCS_MERCURIAL:
    if extra_output is None:
      extra_output = RunShell(["hg", "root"]).strip()
    return MercurialVCS(options, extra_output)
  elif vcs == VCS_SUBVERSION:
    return SubversionVCS(options)
  elif vcs == VCS_GIT:
    return GitVCS(options)

  ErrorExit(("Could not guess version control system. "
             "Are you in a working copy directory?"))


def RealMain(argv, data=None):
  """The real main function.

  Args:
    argv: Command line arguments.
    data: Diff contents. If None (default) the diff is generated by
      the VersionControlSystem implementation returned by GuessVCS().

  Returns:
    A 2-tuple (issue id, patchset id).
    The patchset id is None if the base files are not uploaded by this
    script (applies only to SVN checkouts).
  """
  logging.basicConfig(format=("%(asctime).19s %(levelname)s %(filename)s:"
                              "%(lineno)s %(message)s "))
  os.environ['LC_ALL'] = 'C'
  options, args = parser.parse_args(argv[1:])
  global verbosity
  verbosity = options.verbose
  if verbosity >= 3:
    logging.getLogger().setLevel(logging.DEBUG)
  elif verbosity >= 2:
    logging.getLogger().setLevel(logging.INFO)
  vcs = GuessVCS(options)
  if isinstance(vcs, SubversionVCS):
    # base field is only allowed for Subversion.
    # Note: Fetching base files may become deprecated in future releases.
    base = vcs.GuessBase(options.download_base)
  else:
    base = None
  if not base and options.download_base:
    options.download_base = True
    logging.info("Enabled upload of base file")
  if not options.assume_yes:
    vcs.CheckForUnknownFiles()
  if data is None:
    data = vcs.GenerateDiff(args)
  files = vcs.GetBaseFiles(data)
  if verbosity >= 1:
    print "Upload server:", options.server, "(change with -s/--server)"
  if options.issue:
    prompt = "Message describing this patch set: "
  else:
    prompt = "New issue subject: "
  message = options.message or raw_input(prompt).strip()
  if not message:
    ErrorExit("A non-empty message is required")
  rpc_server = GetRpcServer(options)
  form_fields = [("subject", message)]
  if base:
    form_fields.append(("base", base))
  if options.issue:
    form_fields.append(("issue", str(options.issue)))
  if options.email:
    form_fields.append(("user", options.email))
  if options.reviewers:
    for reviewer in options.reviewers.split(','):
      if "@" in reviewer and not reviewer.split("@")[1].count(".") == 1:
        ErrorExit("Invalid email address: %s" % reviewer)
    form_fields.append(("reviewers", options.reviewers))
  if options.cc:
    for cc in options.cc.split(','):
      if "@" in cc and not cc.split("@")[1].count(".") == 1:
        ErrorExit("Invalid email address: %s" % cc)
    form_fields.append(("cc", options.cc))
  description = options.description
  if options.description_file:
    if options.description:
      ErrorExit("Can't specify description and description_file")
    file = open(options.description_file, 'r')
    description = file.read()
    file.close()
  if description:
    form_fields.append(("description", description))
  # Send a hash of all the base file so the server can determine if a copy
  # already exists in an earlier patchset.
  base_hashes = ""
  for file, info in files.iteritems():
    if not info[0] is None:
      checksum = md5(info[0]).hexdigest()
      if base_hashes:
        base_hashes += "|"
      base_hashes += checksum + ":" + file
  form_fields.append(("base_hashes", base_hashes))
  if options.private:
    if options.issue:
      print "Warning: Private flag ignored when updating an existing issue."
    else:
      form_fields.append(("private", "1"))
  # If we're uploading base files, don't send the email before the uploads, so
  # that it contains the file status.
  if options.send_mail and options.download_base:
    form_fields.append(("send_mail", "1"))
  if not options.download_base:
    form_fields.append(("content_upload", "1"))
  if len(data) > MAX_UPLOAD_SIZE:
    print "Patch is large, so uploading file patches separately."
    uploaded_diff_file = []
    form_fields.append(("separate_patches", "1"))
  else:
    uploaded_diff_file = [("data", "data.diff", data)]
  ctype, body = EncodeMultipartFormData(form_fields, uploaded_diff_file)
  response_body = rpc_server.Send("/upload", body, content_type=ctype)
  patchset = None
  if not options.download_base or not uploaded_diff_file:
    lines = response_body.splitlines()
    if len(lines) >= 2:
      msg = lines[0]
      patchset = lines[1].strip()
      patches = [x.split(" ", 1) for x in lines[2:]]
    else:
      msg = response_body
  else:
    msg = response_body
  StatusUpdate(msg)
  if not response_body.startswith("Issue created.") and \
  not response_body.startswith("Issue updated."):
    sys.exit(0)
  issue = msg[msg.rfind("/")+1:]

  if not uploaded_diff_file:
    result = UploadSeparatePatches(issue, rpc_server, patchset, data, options)
    if not options.download_base:
      patches = result

  if not options.download_base:
    vcs.UploadBaseFiles(issue, rpc_server, patches, patchset, options, files)
    if options.send_mail:
      rpc_server.Send("/" + issue + "/mail", payload="")
  return issue, patchset


def main():
  try:
    RealMain(sys.argv)
  except KeyboardInterrupt:
    print
    StatusUpdate("Interrupted.")
    sys.exit(1)

