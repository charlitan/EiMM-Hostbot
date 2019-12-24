from sqlalchemy import ForeignKey
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class Server(Base):
    __tablename__ = 'Server'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    sheet = Column(String)
    rolepms_id = Column(Integer)
    roles = relationship('Role', back_populates='server')
    channels = relationship('Channel', back_populates='server')
    # rolepms = relationship('RolePMs', back_populates='server', uselist=False)

    def __repr__(self):
        return f'<Server id={self.id}, name={self.name}, sheet={self.sheet}>'


class Role(Base):
    __tablename__ = 'Role'
    id = Column(Integer, primary_key=True)
    type = Column(String)
    server_id = Column(Integer, ForeignKey('Server.id'))
    server = relationship('Server', back_populates='roles')

    def __repr__(self):
        return (
            f'<Role id={self.id}, '
            f'type={self.type}, '
            f'server_id={self.server_id}>'
        )


class Channel(Base):
    __tablename__ = 'Channel'
    id = Column(Integer, primary_key=True)
    type = Column(String)
    server_id = Column(Integer, ForeignKey('Server.id'))
    server = relationship('Server', back_populates='channels')

    def __repr__(self):
        return (
            f'<Channel id={self.id}, '
            f'type={self.type}, '
            f'server_id={self.server_id}>'
        )


# class RolePMs(Base):
#     __tablename__ = 'RolePMs'
#     # id = Column(Integer, primary_key=True)
#     # server_id = Column(Integer, ForeignKey('Server.id'))
#     id = Column(Integer)
#     server_id = Column(Integer, ForeignKey('Server.id'), primary_key=True)
#     server = relationship('Server', back_populates='rolepms')
#
#     def __repr__(self):
#         return (
#             f'<Channel id={self.id}, '
#             f'server_id={self.server_id}>'
#         )
