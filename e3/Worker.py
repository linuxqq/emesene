'''a thread that handles the connection with the main server'''

import time
import Queue
import struct
import urllib
import md5
import httplib
import urlparse
import threading
import xml.sax.saxutils

import XmlManager
import Conversation
import Requester
import protocol.base.Event as Event
import protocol.base.Action as Action
import protocol.base.status as status

EVENTS = (\
 'login started'         , 'login info'           , 
 'login succeed'         , 'login failed'         , 
 'disconnected'          , 'user list ready'      ,
 'contact attr changed'  , 'contact added'        , 
 'contact add succeed'   , 'contact add failed'   , 
 'contact remove succeed', 'contact remove failed', 
 'contact move succeed'  , 'contact move failed'  , 
 'contact copy succeed'  , 'contact copy failed'  , 
 'contact block succeed' , 'contact block failed' , 
 'contact alias succeed' , 'contact alias failed' , 
 'group add succeed'     , 'group add failed'     , 
 'group remove succeed'  , 'group remove failed'  , 
 'group rename succeed'  , 'group rename failed'  ,  
 'status change succeed' , 'status change failed' , 
 'nick change succeed'   , 'nick change failed'   , 
 'message change succeed', 'message change failed', 
 'picture change succeed', 'error'                ,
 'conv contact joined'   , 'conv contact left'  ,
 'conv started'          , 'conv ended'           ,
 'conv group started'    , 'conv group ended'     ,
 'conv message'          , )

ACTIONS = (\
 'login'            , 'logout'           ,
 'change status'    , 'block contact'    ,
 'add contact'      , 'remove contact'   ,
 'set contact alias', 'quit'             ,
 'add to group'     , 'remove from group',
 'move to group'    , 'rename group'     ,
 'add group'        , 'remove group'     ,
 'set nick'         , 'set message'      ,
 'set picture'      , 'set preferences'  ,
 'new conversation' , 'send message')

Event.set_constants(EVENTS)
Action.set_constants(ACTIONS)

STATUS_MAP = {}
STATUS_MAP[status.ONLINE] = 'NLN'
STATUS_MAP[status.OFFLINE] = 'HDN'
STATUS_MAP[status.BUSY] = 'BSY'
STATUS_MAP[status.AWAY] = 'AWY'
STATUS_MAP[status.IDLE] = 'IDL'

STATUS_MAP_REVERSE = {}
STATUS_MAP_REVERSE['NLN'] = status.ONLINE
STATUS_MAP_REVERSE['HDN'] = status.OFFLINE
STATUS_MAP_REVERSE['BSY'] = status.BUSY
STATUS_MAP_REVERSE['AWY'] = status.AWAY
STATUS_MAP_REVERSE['IDL'] = status.IDLE

CLIENT_ID = 0x50000000 | 0x2  # msnc5 + reset capabilities
CLIENT_ID |= 0x4     # ink
CLIENT_ID |= 0x20    # multi-packet MIME messages
CLIENT_ID |= 0x8000  # winks
CLIENT_ID |= 0x40000 # voice clips

def get_value_between(string_, start, stop, default=''):
    '''get the value of string_ between start and stop, return default if
    the value cant be extracted. If multiple time appear start on string_
    just the first will be used, if start or stop are not found default will
    be returned'''
    parts = string_.split(start, 1)

    if len(parts) != 2:
        return default

    parts = parts[1].split(stop, 1)
    
    if len(parts) != 2:
        return default

    return parts[0]

dic = {
    '\"'    :    '&quot;',
    '\''    :    '&apos;'
}

dic_inv = {
    '&quot;'    :'\"',
    '&apos;'    :'\''
}

def escape(string_):
    '''replace the values on dic keys with the values'''
    return xml.sax.saxutils.escape(string_, dic)

def unescape(string_):
    '''replace the values on dic_inv keys with the values'''
    return xml.sax.saxutils.unescape(string_, dic_inv)


class Worker(threading.Thread):
    '''this class represent an object that waits for commands from the queue 
    of a socket, process them and add it as events to its own queue'''

    def __init__(self, socket, session):
        '''class constructor'''
        threading.Thread.__init__(self)
        self.socket = socket
        self.setDaemon(True)

        self.in_login = False
        self.session = session

        self.last_ping_response = 0.0
        self.cid = 0

        # this queue receives a Command object
        self.command_queue = Queue.Queue()

        self.action_handlers = {}
        self._login_handlers = None
        self._common_handlers = None
        self._set_handlers()

        self.conversations = {}
        # contains the tid when the switchboard was requested as key and the
        # account that should be invited when we get the switchboard as value   
        self.pending_conversations = {}

    def _set_handlers(self):
        '''set a dict with the action id as key and the handler as value'''
        dah = {}

        dah[Action.ACTION_ADD_CONTACT] = self._handle_action_add_contact
        dah[Action.ACTION_ADD_GROUP] = self._handle_action_add_group
        dah[Action.ACTION_ADD_TO_GROUP] = self._handle_action_add_to_group
        dah[Action.ACTION_BLOCK_CONTACT] = self._handle_action_block_contact
        dah[Action.ACTION_CHANGE_STATUS] = self._handle_action_change_status
        dah[Action.ACTION_LOGIN] = self._handle_action_login
        dah[Action.ACTION_LOGOUT] = self._handle_action_logout
        dah[Action.ACTION_MOVE_TO_GROUP] = self._handle_action_move_to_group
        dah[Action.ACTION_REMOVE_CONTACT] = self._handle_action_remove_contact
        dah[Action.ACTION_REMOVE_FROM_GROUP] = \
            self._handle_action_remove_from_group
        dah[Action.ACTION_REMOVE_GROUP] = self._handle_action_remove_group
        dah[Action.ACTION_RENAME_GROUP] = self._handle_action_rename_group
        dah[Action.ACTION_SET_CONTACT_ALIAS] = \
            self._handle_action_set_contact_alias
        dah[Action.ACTION_SET_MESSAGE] = self._handle_action_set_message
        dah[Action.ACTION_SET_NICK] = self._handle_action_set_nick
        dah[Action.ACTION_SET_PICTURE] = self._handle_action_set_picture
        dah[Action.ACTION_SET_PREFERENCES] = self._handle_action_set_preferences
        dah[Action.ACTION_NEW_CONVERSATION] = \
            self._handle_action_new_conversation

        self.action_handlers = dah

        # login message handlers

        login_handlers = {}

        login_handlers['VER'] = self._on_version
        login_handlers['CVR'] = self._on_client_version
        login_handlers['XFR'] = self._on_transfer
        login_handlers['USR'] = self._on_user
        login_handlers['SBS'] = self._on_sbs
        login_handlers['MSG'] = self._on_login_message

        self._login_handlers = login_handlers

        # common message handlers

        common_handlers = {}

        common_handlers['ILN'] = self._on_initial_status_change
        common_handlers['UBX'] = self._on_information_change
        common_handlers['CHL'] = self._on_challenge
        common_handlers['NLN'] = self._on_online_change
        common_handlers['FLN'] = self._on_offline_change
        common_handlers['RNG'] = self._on_conversation_invitation
        common_handlers['ADL'] = self._on_add
        common_handlers['RML'] = self._on_remove
        common_handlers['XFR'] = self._on_conversation_transfer
        common_handlers['MSG'] = self._on_server_message
        common_handlers['NOT'] = self._on_notification
        common_handlers['OUT'] = self._on_server_disconnection
        common_handlers['QNG'] = self._on_ping_response
        common_handlers['RNG'] = self._on_conversation_request

        self._common_handlers = common_handlers

    def run(self):
        '''main method, block waiting for data, process it, and send data back 
        to the socket or add a new event to the socket depending on the data'''
        data = None

        while True:
            try:
                data = self.socket.output.get(True, 0.1)
                self._process(data)
            except Queue.Empty:
                pass

            try:
                cmd = self.command_queue.get(True, 0.1)
                self.socket.send_command(cmd.command, cmd.params, cmd.payload)
            except Queue.Empty:
                pass
            
            if self.in_login:
                continue

            try:
                action = self.session.actions.get(True, 0.1)

                if action.id_ == Action.ACTION_QUIT:
                    print 'closing thread'
                    self.socket.input.put('quit')
                    break

                self._process_action(action)
            except Queue.Empty:
                pass

    def _process(self, message):
        '''process the data'''
        print '<<<', message

        if self.in_login:
            self._process_login(message)
        else:
            self._process_normal(message)

    def _process_login(self, message):
        '''handle the messages on the login stage'''
        handler = self._login_handlers.get(message.command, None)

        if handler:
            handler(message)
        else:
            self._on_unknown_command(message)

    def _process_normal(self, message):
        '''handle the messages on normal connection'''
        handler = self._common_handlers.get(message.command, None)

        if handler:
            handler(message)
        else:
            self._on_unknown_command(message)

    def _process_action(self, action):
        '''process an action'''
        if action.id_ in self.action_handlers:
            try:
                self.action_handlers[action.id_](*action.args)
            except TypeError:
                self.session.add_event(Event.EVENT_ERROR, 
                    'Error calling action handler', action.id_)

    def do_passport_identification(self):
        '''do the passport identification and get our passport id'''
        hash_ = self.session.extras['hash'].replace(',', '&amp;')
        template = XmlManager.get('passport')
        # TODO: see if the quote here works
        template = template % (self.session.account.account,
                                urllib.quote(self.session.account.password),
                                hash_)

        #create the headers
        headers = { \
        'Accept' :  'text/*',
        'User-Agent' : 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)',
        'Host' : 'loginnet.passport.com',
        'Content-Length' : str(len(template)),
        'Connection' : 'Keep-Alive',
        'Cache-Control' : 'no-cache'
        }

        server = 'loginnet.passport.com'
        url = '/RST.srf'
        succeeded = False

        for i in range(5):
            response = None

            # send the SOAP request
            for i in range(3):
                try:
                    conn = httplib.HTTPSConnection(server, 443)
                    conn.request('POST', url, template, headers)
                    response = conn.getresponse()
                    break
                except Exception, exception:
                    pass

            if response:
                data = response.read()
            else:
                self.session.add_event(Event.EVENT_LOGIN_FAILED,
                 'Can\'t connect to HTTPS server: ' + str(exception))

            if data.find('<faultcode>psf:Redirect</faultcode>') > 0:
                url = urlparse.urlparse(data.split('<psf:redirectUrl>')\
                    [1].split('</psf:redirectUrl>')[0])

                server = url[1]
                url = url[2]
            else:
                succeeded = True
                break
        
        if not succeeded:
            self.session.add_event(Event.EVENT_LOGIN_FAILED, 
                'Too many redirections')

        # try to get the ticket from the received data
        try:
            twnticket = data.split(
                '<wsse:BinarySecurityToken Id="PPToken1">')[1].split(
                    '</wsse:BinarySecurityToken>')[0]
            twnticket = twnticket.replace('&amp;','&')
        except Exception, exception:
            # try to get the faultstring
            try:
                faultstring = data.split('<faultstring>')\
                    [1].split('</faultstring>')[0]
            except IndexError:
                faultstring = str(exception)

            return self.session.add_event(Event.EVENT_LOGIN_FAILED, faultstring)

        return twnticket

    def set_status(self, stat):
        '''set our status'''
        self.session.account.status = stat
        self.session.contacts.me.status = stat
        self.socket.send_command('CHG', (STATUS_MAP[stat], str(CLIENT_ID), '0'))

    def _on_version(self, message):
        '''handle version'''
        self.socket.send_command('CVR', 
            ('0x0c0a', 'winnt', '5.1', 'i386', 'MSNMSGR', '8.0.0792', 
            'msmsgs', self.session.account.account))

    def _on_client_version(self, message):
        '''handle client version'''
        self.socket.send_command('USR', ('TWN', 'I', 
            self.session.account.account))

    def _on_transfer(self, message):
        '''handle server transfer'''
        if message.param_num_is(0, 'NS') and \
            message.param_num_exists(1):

            try:
                (host, port) = message.params[1].split(':')
            except ValueError:
                self.session.add_event(Event.EVENT_LOGIN_FAILED,
                    'invalid XFR command')

            self.socket.reconnect(host, int(port))
            self.socket.send_command('VER', ('MSNP13', 'CVR0'))
        else:
            self.session.add_event(Event.EVENT_LOGIN_FAILED, 
                'invalid XFR command')

    def _on_user(self, message):
        '''handle user response'''
        if message.param_num_is(0, 'TWN'):
            hash_ = ' '.join(message.params[2:])
            self.session.extras['hash'] = urllib.unquote(hash_)
            passport_id = self.do_passport_identification()
            self.session.extras['t'] = passport_id.split('&p=')[0][2:]
            self.session.extras['MSPProf'] = passport_id.split('&p=')[1]

            # if returned a tuple with a signal, we return it
            if type(passport_id) == tuple:
                return passport_id

            self.session.extras['passport id'] = passport_id

            try:
                ticket = passport_id.split('&p=')[0][2:]
                param = passport_id.split('&p=')[1]
                self.session.extras['t'] = ticket
                self.session.extras['p'] = param
            except IndexError:
                self.session.add_event(Event.EVENT_LOGIN_FAILED, 
                    'Incorrect passport id')

            # we introduce ourselves again
            self.socket.send_command('USR', ('TWN', 'S', \
                passport_id))
        elif message.param_num_is(0, 'OK'):
            pass       

    def _on_sbs(self, message):
        '''handle (or dont) the sbs message'''
        pass

    def _on_login_message(self, message):
        '''handle server message on login'''
        for line in message.payload.split('\r\n'):
            if line:
                (key, value) = line.split(': ')
                self.session.extras[key] = value

        self.session.add_event(Event.EVENT_LOGIN_SUCCEED)
        self.in_login = False
        self.set_status(self.session.account.status)
        Requester.Membership(self.session, self.command_queue, 
            True).start()
                
    def _on_initial_status_change(self, message):
        '''handle the first status change of the contacts, that means
        the status of the contacts that were connected before our connection'''

        param_length = len(message.params)
        (status_, email, nick) = message.params[:3]
        msnobj = None

        if param_length == 5:
            msnobj = urllib.unquote(message.params[4])

        email.lower()
        status_ = STATUS_MAP_REVERSE[status_]
        nick = urllib.unquote(nick)
        nick = nick.decode('utf-8', 'replace').encode('utf-8')

        contact = self.session.contacts.contacts.get(email, None)

        if not contact:
            return

        contact.status = status_
        contact.nick = nick
        contact.attrs['msnobj'] = msnobj
        
        #import pprint
        #pprint.pprint(self.session.contacts.get_sorted_list_by_status())
        # don't genetate an event here, because it's after the client
        # requests the contact list

    def _on_information_change(self, message):
        '''handle the change of the information of a contact (personal 
        message)'''
        if int(message.params[0]) == 0:
            return

        pmessage = get_value_between(message.payload, '<PSM>', '</PSM>')
        pmessage = unescape(pmessage)
        account = message.tid

        contact = self.session.contacts.contacts.get(account, None)

        if not contact:
            return

        contact.message = pmessage
       
        media = get_value_between(message.payload, '<CurrentMedia>', 
            '<CurrentMedia>')

        mhead = media.find('\\0Music\\01\\0')

        if mhead != -1:
            media = media[mhead+12:]
            margs = media.split('\\0')
            media = margs[0]

            for args in range(1, len(margs)):
                media = media.replace('{%s}' % (args-1), margs[args])

            media = unescape(media)
                
            contact.media = media

        self.session.add_event(Event.EVENT_CONTACT_ATTR_CHANGED, account)

    def _on_challenge(self, message):
        '''handle the challenge sent by the server'''
        out = doChallenge(message.params[0][:-2])
        self.socket.send_command('QRY', (_PRODUCT_ID,) , out)

    def _on_online_change(self, message):
        '''handle the status change of a contact that comes from offline'''
        status_ = STATUS_MAP_REVERSE[message.tid]
        account = message.params[0].lower()
        nick = message.params[0]
        params_length = len(message.params)

        nick = urllib.unquote(nick)
        nick = nick.decode('utf-8', 'replace').encode('utf-8')
        
        contact = self.session.contacts.contacts.get(account, None)
        
        if not contact:
            return

        contact.status = status_
        contact.nick = nick

        if params_length == 4:
            msnobj = urllib.unquote(message.params[3])
            contact.attrs['CID'] = int(message.params[2])
        
        self.session.add_event(Event.EVENT_CONTACT_ATTR_CHANGED, account)

        # TODO: here we should check the old and the new msnobj and request the
        # new image if needed

    def _on_offline_change(self, message):
        '''handle the disconnection of a contact'''
        account = message.tid
        contact = self.session.contacts.contacts.get(account, None)
        
        if not contact:
            return

        contact.status = status.OFFLINE
        
        self.session.add_event(Event.EVENT_CONTACT_ATTR_CHANGED, account)

    def _on_conversation_invitation(self, message):
        '''handle the invitation to start a conversation'''
        pass

    def _on_add(self, message):
        '''handle a message that informs us about someone that added us'''
        pass

    def _on_remove(self, message):
        '''handle a message that informs us about someone that removed us'''
        pass

    def _on_conversation_transfer(self, message):
        '''handle a message that inform us that we must start a new switchboard
        with a server'''
        (sb_, chost, cki, session_id, unk, server) = message.params

        (host, port) = chost.split(':')
        account = self.pending_conversations[int(message.tid)]
        del self.pending_conversations[int(message.tid)]

        cid = self.cid
        self.cid += 1
        con = Conversation.Conversation(self.session, cid, host, int(port), 
            account, session_id)
        self.conversations[cid] = con
        con.send_presentation()
        con.invite(account)
        con.start()

    def _on_server_message(self, message):
        '''handle a server message when we are not on the login stage'''
        print 'server message:', message.payload

    def _on_notification(self, message):
        '''handle a notification message'''
        print 'server notification:', message.payload

    def _on_server_disconnection(self, message):
        '''handle the message that inform us that we were disconnected from
        the server'''
        # TODO: translate the reasons to meaninful messages
        self.session.add_event(Event.EVENT_DISCONNECTED, message.tid)

    def _on_ping_response(self, message):
        '''handle the response from the PNG command'''
        self.last_ping_response = time.time()

    def _on_conversation_request(self, message):
        '''handle a conversation request, example
        RNG 1581441881 64.4.37.33:1863 CKI 252199185.167235214 eltuza@gmail.com tuza U messenger.msn.com'''
        session_id = message.tid
        (chost, auth_type, auth_id, user, username, unk, server) = \
        message.params

        (host, port) = chost.split(':')

        cid = self.cid
        self.cid += 1
        con = Conversation.Conversation(self.session, cid, host, int(port), 
            user, session_id, auth_id)
        self.conversations[cid] = con
        con.answer()
        con.start()

    def _on_unknown_command(self, message):
        '''handle the unknown commands'''
        print 'unknown command:', str(message)

    # action handlers
    def _handle_action_add_contact(self, account):
        '''handle Action.ACTION_ADD_CONTACT
        '''
        pass

    def _handle_action_add_group(self, name):
        '''handle Action.ACTION_ADD_GROUP
        '''
        pass

    def _handle_action_add_to_group(self, account, gid):
        '''handle Action.ACTION_ADD_TO_GROUP
        '''
        pass

    def _handle_action_block_contact(self, account):
        '''handle Action.ACTION_BLOCK_CONTACT
        '''
        pass

    def _handle_action_change_status(self, status_):
        '''handle Action.ACTION_CHANGE_STATUS
        '''
        pass

    def _handle_action_login(self, account, password, status_):
        '''handle Action.ACTION_LOGIN
        '''
        self.session.account.account = account
        self.session.account.password = password
        self.session.account.status = status_

        self.socket.send_command('VER', ('MSNP13', 'CVR0'))
        self.session.add_event(Event.EVENT_LOGIN_STARTED)
        self.in_login = True

    def _handle_action_logout(self):
        '''handle Action.ACTION_LOGOUT
        '''
        pass

    def _handle_action_move_to_group(self, account, src_gid, dest_gid):
        '''handle Action.ACTION_MOVE_TO_GROUP
        '''
        pass

    def _handle_action_remove_contact(self, account):
        '''handle Action.ACTION_REMOVE_CONTACT
        '''
        pass

    def _handle_action_remove_from_group(self, account, gid):
        '''handle Action.ACTION_REMOVE_FROM_GROUP
        '''
        pass

    def _handle_action_remove_group(self, gid):
        '''handle Action.ACTION_REMOVE_GROUP
        '''
        pass

    def _handle_action_rename_group(self, gid, name):
        '''handle Action.ACTION_RENAME_GROUP
        '''
        pass

    def _handle_action_set_contact_alias(self, account, alias):
        '''handle Action.ACTION_SET_CONTACT_ALIAS
        '''
        pass

    def _handle_action_set_message(self, message):
        '''handle Action.ACTION_SET_MESSAGE
        '''
        pass

    def _handle_action_set_nick(self, nick):
        '''handle Action.ACTION_SET_NICK
        '''
        pass

    def _handle_action_set_picture(self, picture_name):
        '''handle Action.ACTION_SET_PICTURE
        '''
        pass

    def _handle_action_set_preferences(self, preferences):
        '''handle Action.ACTION_SET_PREFERENCES
        '''
        pass

    def _handle_action_new_conversation(self, account):
        '''handle Action.ACTION_NEW_CONVERSATION
        '''
        self.pending_conversations[self.socket.tid] = account
        self.socket.send_command('XFR', ('SB',))

    def _handle_action_send_message(self, cid, message):
        '''handle Action.ACTION_SEND_MESSAGE
        cid is the conversation id, message is a MsnMessage object
        '''
        if cid not in self.conversations:
            self.session.add_event(Event.EVENT_ERROR, 'invalid conversation id')
            return

        self.conversations[cid].send_message(message)

#------------------------- FROM HERE -----------------------------
# Copyright 2005 James Bunton <james@delx.cjb.net>
# Licensed for distribution under the GPL version 2, check COPYING for details
_PRODUCT_KEY = 'O4BG@C7BWLYQX?5G'
_PRODUCT_ID = 'PROD01065C%ZFN6F' 
MSNP11_MAGIC_NUM = 0x0E79A9C1

def doChallenge(chlData):
    md5digest = md5.md5(chlData + _PRODUCT_KEY).digest()

    # Make array of md5 string ints
    md5Ints = struct.unpack("<llll", md5digest)
    md5Ints = [(x & 0x7fffffff) for x in md5Ints]

    # Make array of chl string ints
    chlData += _PRODUCT_ID
    amount = 8 - len(chlData) % 8
    chlData += "".zfill(amount)
    chlInts = struct.unpack("<%di" % (len(chlData)/4), chlData)

    # Make the key
    high = 0
    low = 0
    i = 0
    while i < len(chlInts) - 1:
        temp = chlInts[i]
        temp = (MSNP11_MAGIC_NUM * temp) % 0x7FFFFFFF
        temp += high
        temp = md5Ints[0] * temp + md5Ints[1]
        temp = temp % 0x7FFFFFFF

        high = chlInts[i + 1]
        high = (high + temp) % 0x7FFFFFFF
        high = md5Ints[2] * high + md5Ints[3]
        high = high % 0x7FFFFFFF

        low = low + high + temp

        i += 2

    high = littleEndify((high + md5Ints[1]) % 0x7FFFFFFF)
    low = littleEndify((low + md5Ints[3]) % 0x7FFFFFFF)
    key = (high << 32L) + low
    key = littleEndify(key, "Q")

    longs = [x for x in struct.unpack(">QQ", md5digest)]
    longs = [littleEndify(x, "Q") for x in longs]
    longs = [x ^ key for x in longs]
    longs = [littleEndify(abs(x), "Q") for x in longs]
    
    out = ""
    for x in longs:
        x = hex(long(x))
        x = x[2:-1]
        x = x.zfill(16)
        out += x.lower()
    
    return out

def littleEndify(num, c="L"):
    return struct.unpack(">" + c, struct.pack("<" + c, num))[0]
# ------------------------------ TO HERE ----------------------------------
