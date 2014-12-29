import collections
from copy import copy
import Queue
from threading import RLock, Thread

from pychess.Utils.Move import *
from pychess.Utils.Board import Board
from pychess.Utils.Cord import Cord
from pychess.Utils.Offer import Offer
from pychess.Utils.Move import toAN, parseAN, listToMoves
from pychess.Utils.logic import validate, getMoveKillingKing, getStatus, legalMoveCount
from pychess.Utils.const import *
from pychess.Utils.lutils.ldata import MATE_VALUE
from pychess.Utils.lutils.lmove import ParsingError
from pychess.System import conf, fident
from pychess.System.Log import log
from pychess.System.SubProcess import TimeOutError, SubProcessError
from pychess.Variants.fischerandom import FischerRandomChess

from ProtocolEngine import ProtocolEngine
from pychess.Players.Player import Player, PlayerIsDead, TurnInterrupt

TYPEDIC = {"check":lambda x:x=="true", "spin":int}
OPTKEYS = ("name", "type", "min", "max", "default", "var")

class UCIEngine (ProtocolEngine):
    
    def __init__ (self, subprocess, color, protover, md5):
        ProtocolEngine.__init__(self, subprocess, color, protover, md5)
        
        self.ids = {}
        self.options = {}
        self.optionsToBeSent = {}
        
        self.wtime = 60000
        self.btime = 60000
        self.incr = 0
        self.timeHandicap = 1 
        
        self.moveLock = RLock()
        # none of the following variables should be changed or used in a
        # condition statement without holding the above self.moveLock
        self.ponderOn = False
        self.pondermove = None
        self.ignoreNext = False
        self.waitingForMove = False
        self.needBestmove = False
        self.readyForStop = False   # keeps track of whether we already sent a 'stop' command
        self.multipvSetting  = conf.get("multipv", 1)    # MultiPV option sent to the engine
        self.multipvExpected = 1    # Number of PVs expected (limited by number of legal moves)
        self.commands = collections.deque()
        
        self.gameBoard = Board(setup=True) # board at the end of all moves played
        self.board = Board(setup=True)     # board to send the engine
        self.uciPosition = "startpos"
        self.uciPositionListsMoves = False
        self.analysis = [ None ]
        
        self.returnQueue = Queue.Queue()
        self.engine.connect("line", self.parseLines)
        self.engine.connect("died", self.__die)
        
        self.connect("readyForOptions", self.__onReadyForOptions_before)
        self.connect_after("readyForOptions", self.__onReadyForOptions)
        self.connect_after("readyForMoves", self.__onReadyForMoves)
    
    def __die (self, subprocess):
        self.returnQueue.put("die")
    
    #===========================================================================
    #    Starting the game
    #===========================================================================
    
    def prestart (self):
        print >> self.engine, "uci"
    
    def start (self):
        if self.mode in (ANALYZING, INVERSE_ANALYZING):
            t = Thread(target=self.__startBlocking,
                       name=fident(self.__startBlocking))
            t.daemon = True
            t.start()
        else:
            self.__startBlocking()
    
    def __startBlocking (self):
        r = self.returnQueue.get()
        if r == 'die':
            raise PlayerIsDead
        assert r == "ready" or r == 'del'
        #self.emit("readyForOptions")
        #self.emit("readyForMoves")
    
    def __onReadyForOptions_before (self, self_):
        self.readyOptions = True
    
    def __onReadyForOptions (self, self_):
        if self.mode in (ANALYZING, INVERSE_ANALYZING):
            if self.hasOption("Ponder"):
                self.setOption('Ponder', False)
        
            if self.hasOption("MultiPV") and self.multipvSetting > 1:
                self.setOption('MultiPV', self.multipvSetting)
            
        for option, value in self.optionsToBeSent.iteritems():
            if type(value) == bool:
                value = str(value).lower()
            print >> self.engine, "setoption name %s value %s" % (option, str(value))
        
        print >> self.engine, "isready"
    
    def __onReadyForMoves (self, self_):
        self.returnQueue.put("ready")
        self.readyMoves = True
        self._newGame()
        
        # If we are an analyzer, this signal was already called in a different
        # thread, so we can safely block it.
        if self.mode in (ANALYZING, INVERSE_ANALYZING):
            self._searchNow()
    
    #===========================================================================
    #    Ending the game
    #===========================================================================
    
    def end (self, status, reason):
        # UCI doens't care about reason, so we just kill
        self.kill(reason)
    
    def kill (self, reason):
        """ Kills the engine, starting with the 'stop' and 'quit' commands, then
            trying sigterm and eventually sigkill.
            Returns the exitcode, or if engine have already been killed, the
            method returns None """
        if self.connected:
            self.connected = False
            try:
                try:
                    print >> self.engine, "stop"
                    print >> self.engine, "quit"
                    self.returnQueue.put("del")
                    return self.engine.gentleKill()
                
                except OSError, e:
                    # No need to raise on a hang up error, as the engine is dead
                    # anyways
                    if e.errno == 32:
                        log.warning("Hung up Error", extra={"task":self.defname})
                        return e.errno
                    else: raise
            
            finally:
                # Clear the analyzed data, if any
                self.emit("analyze", [])
    
    #===========================================================================
    #    Send the player move updates
    #===========================================================================
    
    def _moveToUCI (self, board, move):
        cn = CASTLE_KK
        if board.variant == FISCHERRANDOMCHESS:
            cn = CASTLE_KR
        return toAN(board, move, short=True, castleNotation=cn)
    
    def _recordMove (self, board1, move, board2):
        if self.gameBoard == board1:
            return
        if not board2:
            if board1.variant == NORMALCHESS and board1.asFen() == FEN_START:
                self.uciPosition = "startpos"
            else:
                self.uciPosition = "fen " + board1.asFen()
            self.uciPositionListsMoves = False
        if move:
            if not self.uciPositionListsMoves:
                self.uciPosition += " moves"
                self.uciPositionListsMoves = True
            self.uciPosition += " " + self._moveToUCI(board2, move)


        self.board = self.gameBoard = board1
        if self.mode == INVERSE_ANALYZING:
            self.board = self.gameBoard.switchColor()

    def _recordMoveList (self, model, ply=None):
        self._recordMove(model.boards[0], None, None)
        if ply is None:
            ply = model.ply
        for board1, move, board2 in zip(model.boards[1:ply+1], model.moves, model.boards[0:ply]):
            self._recordMove(board1, move, board2)

    
    def setBoard (self, board):
        log.debug("setBoardAtPly: board=%s" % board, extra={"task":self.defname})
        self._recordMove(board, None, None)
        
        if not self.readyMoves:
            return
        self._searchNow()

    def putMove (self, board1, move, board2):
        log.debug("putMove: board1=%s move=%s board2=%s self.board=%s" % \
            (board1, move, board2, self.board), extra={"task":self.defname})
        self._recordMove(board1, move, board2)
        
        if not self.readyMoves:
            return
        self._searchNow()
    
    def makeMove (self, board1, move, board2):
        log.debug("makeMove: move=%s self.pondermove=%s board1=%s board2=%s self.board=%s" % \
            (move, self.pondermove, board1, board2, self.board), extra={"task":self.defname})
        assert self.readyMoves
        
        with self.moveLock:
            self._recordMove(board1, move, board2)
            self.waitingForMove = True
            ponderhit = False
            
            if board2 and self.pondermove and move == self.pondermove:
                ponderhit = True
            elif board2 and self.pondermove:
                self.ignoreNext = True
                print >> self.engine, "stop"
            
            self._searchNow(ponderhit=ponderhit)
        
        # Parse outputs
        try:
            r = self.returnQueue.get()
            if r == "del":
                raise PlayerIsDead
            if r == "int":
                with self.moveLock:
                    self.pondermove = None
                    self.ignoreNext = True
                    self.needBestmove = True
                    self.hurry()
                    raise TurnInterrupt
            return r
        finally:
            with self.moveLock:
                self.waitingForMove = False
                # empty the queue of any moves received post-undo/TurnInterrupt
                self.returnQueue.queue.clear()
    
    def updateTime (self, secs, opsecs):
        if self.color == WHITE:
            self.wtime = int(secs*1000*self.timeHandicap)
            self.btime = int(opsecs*1000)
        else:
            self.btime = int(secs*1000*self.timeHandicap)
            self.wtime = int(opsecs*1000)
    
    #===========================================================================
    #    Standard options
    #===========================================================================
    
    def setOptionAnalyzing (self, mode):
        self.mode = mode
        if self.mode == INVERSE_ANALYZING:
            self.board = self.gameBoard.switchColor()
    
    def setOptionInitialBoard (self, model):
        log.debug("setOptionInitialBoard: self=%s, model=%s" % \
            (self, model), extra={"task":self.defname})
        self._recordMoveList(model)
    
    def setOptionVariant (self, variant):
        if variant == FischerRandomChess:
            assert self.hasOption("UCI_Chess960")
            self.setOption("UCI_Chess960", True)
        elif self.hasOption("UCI_Variant") and not variant.standard_rules:
            self.setOption("UCI_Variant", variant.cecp_name)
    
    def setOptionTime (self, secs, gain):
        self.wtime = int(max(secs*1000*self.timeHandicap, 1))
        self.btime = int(max(secs*1000*self.timeHandicap, 1))
        self.incr = int(gain*1000*self.timeHandicap)
    
    def setOptionStrength (self, strength, forcePonderOff):
        self.strength = strength
        
        if self.hasOption('UCI_LimitStrength') and strength <= 18:
            self.setOption('UCI_LimitStrength', True)
            if self.hasOption('UCI_Elo'):
                self.setOption('UCI_Elo', 150 * strength)
        
        # Stockfish offers 20 skill levels
        if self.hasOption('Skill Level') and strength <= 19:
            self.setOption('Skill Level', strength)

        if ((not self.hasOption('UCI_Elo')) and (not self.hasOption('Skill Level'))) or strength <= 19:
            self.timeHandicap = th = 0.01 * 10**(strength/10.)
            self.wtime = int(max(self.wtime*th, 1))
            self.btime = int(max(self.btime*th, 1))
            self.incr = int(self.incr*th)
        
        if self.hasOption('Ponder'):
            self.setOption('Ponder', strength >= 19 and not forcePonderOff)

        if self.hasOption('GaviotaTbPath') and strength == 20:
            self.setOption('GaviotaTbPath', conf.get("egtb_path", ""))
    #===========================================================================
    #    Interacting with the player
    #===========================================================================
    
    def pause (self):
        log.debug("pause: self=%s" % self, extra={"task":self.defname})
        self.engine.pause()
        return
        
        if self.board.color == self.color or \
                self.mode != NORMAL or self.pondermove:
            self.ignoreNext = True
            print >> self.engine, "stop"
    
    def resume (self):
        log.debug("resume: self=%s" % self, extra={"task":self.defname})
        self.engine.resume()
        return
        
        if self.mode == NORMAL:
            if self.board.color == self.color:
                self._searchNow()
            elif self.ponderOn and self.pondermove:
                self._startPonder()
        else:
            self._searchNow()
    
    def hurry (self):
        log.debug("hurry: self.waitingForMove=%s self.readyForStop=%s" % \
            (self.waitingForMove, self.readyForStop), extra={"task":self.defname})
        # sending this more than once per move will crash most engines
        # so we need to send only the first one, and then ignore every "hurry" request
        # after that until there is another outstanding "position..go"
        with self.moveLock:
            if self.waitingForMove and self.readyForStop:
                print >> self.engine, "stop"
                self.readyForStop = False
    
    def playerUndoMoves (self, moves, gamemodel):
        log.debug("playerUndoMoves: moves=%s gamemodel.ply=%s gamemodel.boards[-1]=%s self.board=%s" % \
            (moves, gamemodel.ply, gamemodel.boards[-1], self.board), extra={"task":self.defname})

        self._recordMoveList(gamemodel)
        
        if (gamemodel.curplayer != self and moves % 2 == 1) or \
                (gamemodel.curplayer == self and moves % 2 == 0):
            # Interrupt if we were searching but should no longer do so, or
            # if it is was our move before undo and it is still our move after undo
            # since we need to send the engine the new FEN in makeMove()
            log.debug("playerUndoMoves: putting 'int' into self.returnQueue=%s" % \
                self.returnQueue.queue, extra={"task":self.defname})
            self.returnQueue.put("int")
    
    def spectatorUndoMoves (self, moves, gamemodel):
        log.debug("spectatorUndoMoves: moves=%s gamemodel.ply=%s gamemodel.boards[-1]=%s self.board=%s" % \
            (moves, gamemodel.ply, gamemodel.boards[-1], self.board), extra={"task":self.defname})

        self._recordMoveList(gamemodel)
        
        if self.readyMoves:
            self._searchNow()
    
    #===========================================================================
    #    Offer handling
    #===========================================================================
    
    def offer (self, offer):
        if offer.type == DRAW_OFFER:
            self.emit("decline", offer)
        else:
            self.emit("accept", offer)
    
    #===========================================================================
    #    Option handling
    #===========================================================================
    
    def setOption (self, key, value):
        """ Set an option, which will be sent to the engine, after the
            'readyForOptions' signal has passed.
            If you want to know the possible options, you should go to
            engineDiscoverer or use the getOption, getOptions and hasOption
            methods, while you are in your 'readyForOptions' signal handler """ 
        if self.readyMoves:
            log.warning("Options set after 'readyok' are not sent to the engine", extra={"task":self.defname})
        self.optionsToBeSent[key] = value
        self.ponderOn = key=="Ponder" and value is True
    
    def getOption (self, option):
        assert self.readyOptions
        if option in self.options:
            return self.options[option]["default"]
        return None
    
    def getOptions (self):
        assert self.readyOptions
        return copy(self.options)
    
    def hasOption (self, key):
        assert self.readyOptions
        return key in self.options
    
    #===========================================================================
    #    Internal
    #===========================================================================
    
    def _newGame (self):
        print >> self.engine, "ucinewgame"
    
    def _searchNow (self, ponderhit=False):
        log.debug("_searchNow: self.needBestmove=%s ponderhit=%s self.board=%s" % \
            (self.needBestmove, ponderhit, self.board), extra={"task":self.defname})

        with self.moveLock:
            commands = []
            
            if ponderhit:
                commands.append("ponderhit")
                
            elif self.mode == NORMAL:
                commands.append("position %s" % self.uciPosition)
                if self.strength <= 3:
                    commands.append("go depth %d" % self.strength)
                else:
                    commands.append("go wtime %d winc %d btime %d binc %d" % \
                                    (self.wtime, self.incr, self.btime, self.incr))
                
            else:
                print >> self.engine, "stop"
                
                if self.mode == INVERSE_ANALYZING:
                    if self.board.board.opIsChecked():
                        # Many engines don't like positions able to take down enemy
                        # king. Therefore we just return the "kill king" move
                        # automaticaly
                        self.emit("analyze", [([getMoveKillingKing(self.board)], MATE_VALUE-1, "")])
                        return
                    commands.append("position fen %s" % self.board.asFen())
                else:
                    commands.append("position %s" % self.uciPosition)

                #commands.append("go infinite")
                move_time = int(conf.get("max_analysis_spin", 3))*1000
                commands.append("go movetime %s" % move_time)

            if self.hasOption("MultiPV") and self.multipvSetting > 1:
                self.multipvExpected = min(self.multipvSetting, legalMoveCount(self.board))
            else:
                self.multipvExpected = 1
            self.analysis = [None] * self.multipvExpected
            
            if self.needBestmove:
                self.commands.append(commands)
                log.debug("_searchNow: self.needBestmove==True, appended to self.commands=%s" % \
                    self.commands, extra={"task":self.defname})
            else:
                for command in commands:
                    print >> self.engine, command
                if getStatus(self.board)[1] != WON_MATE: # XXX This looks fishy.
                    self.needBestmove = True
                    self.readyForStop = True
    
    def _startPonder (self):
        uciPos = self.uciPosition
        if not self.uciPositionListsMoves:
            uciPos += " moves"
        print >> self.engine, "position", uciPos, \
                                self._moveToUCI(self.board, self.pondermove)
        print >> self.engine, "go ponder wtime", self.wtime, \
            "winc", self.incr, "btime", self.btime, "binc", self.incr
    
    #===========================================================================
    #    Parsing from engine
    #===========================================================================
    
    def parseLines (self, engine, lines):
        for line in lines:
            self.__parseLine(line)
    
    def __parseLine (self, line):
        if not self.connected: return
        parts = line.split()
        if not parts: return
        
        #---------------------------------------------------------- Initializing
        if parts[0] == "id":
            self.ids[parts[1]] = " ".join(parts[2:])
            if parts[1] == "name":
                self.setName(self.ids["name"])
            return
        
        if parts[0] == "uciok":
            self.emit("readyForOptions")
            return
        
        if parts[0] == "readyok":
            self.emit("readyForMoves")
            return
        
        #------------------------------------------------------- Options parsing
        if parts[0] == "option":
            dic = {}
            last = 1
            varlist = []
            for i in xrange (2, len(parts)+1):
                if i == len(parts) or parts[i] in OPTKEYS:
                    key = parts[last]
                    value = " ".join(parts[last+1:i])
                    if "type" in dic and dic["type"] in TYPEDIC:
                        value = TYPEDIC[dic["type"]](value)
                        
                    if key == "var":
                        varlist.append(value)
                    elif key == "type" and value == "string":
                        dic[key] = "text"
                    else:
                        dic[key] = value
                        
                    last = i
            if varlist:
                dic["choices"] = varlist
            
            self.options[dic["name"]] = dic
            return
        
        #---------------------------------------------------------------- A Move
        if self.mode == NORMAL and parts[0] == "bestmove":
            with self.moveLock:
                self.needBestmove = False
                self.__sendQueuedGo()
                
                if self.ignoreNext:
                    log.debug("__parseLine: line='%s' self.ignoreNext==True, returning" % \
                        line.strip(), extra={"task":self.defname})
                    self.ignoreNext = False
                    self.readyForStop = True
                    return
                
                if not self.waitingForMove:
                    log.warning("__parseLine: self.waitingForMove==False, ignoring move=%s" % \
                        parts[1], extra={"task":self.defname})
                    self.pondermove = None
                    return
                self.waitingForMove = False

                try:
                    move = parseAny(self.board, parts[1])
                except ParsingError, e:
                    self.end(WHITEWON if self.board.color == BLACK else BLACKWON, WON_ADJUDICATION)
                    return
                
                if not validate(self.board, move):
                    # This is critical. To avoid game stalls, we need to resign on
                    # behalf of the engine.
                    log.error("__parseLine: move=%s didn't validate, putting 'del' in returnQueue. self.board=%s" % \
                        (repr(move), self.board), extra={"task":self.defname})
                    self.end(WHITEWON if self.board.color == BLACK else BLACKWON, WON_ADJUDICATION)
                    return
                
                self._recordMove(self.board.move(move), move, self.board)
                log.debug("__parseLine: applied move=%s to self.board=%s" % \
                    (move, self.board), extra={"task":self.defname})
                
                if self.ponderOn:
                    self.pondermove = None
                    # An engine may send an empty ponder line, simply to clear.
                    if len(parts) == 4:
                        # Engines don't always check for everything in their
                        # ponders. Hence we need to validate.
                        # But in some cases, what they send may not even be
                        # correct AN - specially in the case of promotion.
                        try:
                            pondermove = parseAny(self.board, parts[3])
                        except ParsingError:
                            pass
                        else:
                            if validate(self.board, pondermove):
                                self.pondermove = pondermove
                                self._startPonder()
                
                self.returnQueue.put(move)
                log.debug("__parseLine: put move=%s into self.returnQueue=%s" % \
                    (move, self.returnQueue.queue), extra={"task":self.defname})
                return
        
        #----------------------------------------------------------- An Analysis
        if self.mode != NORMAL and parts[0] == "info" and "pv" in parts:
            multipv = 1
            if "multipv" in parts:
                multipv = int(parts[parts.index("multipv")+1])
            scoretype = parts[parts.index("score")+1]
            if scoretype in ('lowerbound', 'upperbound'):
                score = None
            else:
                score = int(parts[parts.index("score")+2])
                if scoretype == 'mate':
#                    print >> self.engine, "stop"
                    if score != 0:
                        sign = score/abs(score)
                        score = sign*MATE_VALUE
            
            movstrs = parts[parts.index("pv")+1:]
            try:
                moves = listToMoves (self.board, movstrs, AN, validate=True, ignoreErrors=False)
            except ParsingError, e:
                # ParsingErrors may happen when parsing "old" lines from
                # analyzing engines, which haven't yet noticed their new tasks
                log.debug("__parseLine: Ignored (%s) from analyzer: ParsingError%s" % \
                    (' '.join(movstrs),e), extra={"task":self.defname})
                return

            if "depth" in parts:
                depth = parts[parts.index("depth")+1]
            else:
                depth = ""
                
            if multipv <= len(self.analysis):
                self.analysis[multipv - 1] = (moves, score, depth)

            self.emit("analyze", self.analysis)
            return
        
        #-----------------------------------------------  An Analyzer bestmove
        if self.mode != NORMAL and parts[0] == "bestmove":
            with self.moveLock:
                log.debug("__parseLine: processing analyzer bestmove='%s'" % \
                    line.strip(), extra={"task":self.defname})
                self.needBestmove = False
                self.__sendQueuedGo(sendlast=True)
                return
        
        #  Stockfish complaining it received a 'stop' without a corresponding 'position..go'
        if line.strip() == "Unknown command: stop":
            with self.moveLock:
                log.debug("__parseLine: processing '%s'" % line.strip(), extra={"task":self.defname})
                self.ignoreNext = False
                self.needBestmove = False
                self.readyForStop = False
                self.__sendQueuedGo()
                return
        
        #* score
        #* cp <x>
        #    the score from the engine's point of view in centipawns.
        #* mate <y>
        #    mate in y moves, not plies.
        #    If the engine is getting mated use negative values for y.
        #* lowerbound
        #  the score is just a lower bound.
        #* upperbound
        #   the score is just an upper bound.
    
    def __sendQueuedGo (self, sendlast=False):
        """ Sends the next position...go or ponderhit command set which was queued (if any).
        
        sendlast -- If True, send the last position-go queued rather than the first,
        and discard the others (intended for analyzers)
        """
        with self.moveLock:
            if len(self.commands) > 0:
                if sendlast:
                    commands = self.commands.pop()
                    self.commands.clear()
                else:
                    commands = self.commands.popleft()
                
                for command in commands:
                    print >> self.engine, command
                self.needBestmove = True
                self.readyForStop = True
                log.debug("__sendQueuedGo: sent queued go=%s" % commands, extra={"task":self.defname})

    #===========================================================================
    #    Info
    #===========================================================================
    
    def maxAnalysisLines (self):
        try:
            return int(self.options["MultiPV"]["max"])
        except (KeyError, ValueError):
            return 1 # Engine does not support the MultiPV option
        
    def requestMultiPV (self, n):
        multipvMax = self.maxAnalysisLines()
        n = min(n, multipvMax)
        
        if n != self.multipvSetting:
            conf.set("multipv", n)
            with self.moveLock:
                self.multipvSetting  = n
                print >> self.engine, "stop"
                print >> self.engine, "setoption name MultiPV value", n
                self._searchNow()
        
        return n
    
    def __repr__ (self):
        if self.name:
            return self.name
        if "name" in self.ids:
            return self.ids["name"]
        return ', '.join(self.defname)
